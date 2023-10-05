import datetime
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from moto.events.responses import EventsHandler as MotoEventsHandler
from werkzeug import Request
from werkzeug.exceptions import NotFound

from localstack import config
from localstack.aws.api import RequestContext
from localstack.aws.api.core import CommonServiceException, ServiceException
from localstack.aws.api.events import (
    Boolean,
    ConnectionAuthorizationType,
    ConnectionDescription,
    ConnectionName,
    CreateConnectionAuthRequestParameters,
    CreateConnectionResponse,
    EventBusNameOrArn,
    EventPattern,
    EventsApi,
    PutRuleResponse,
    PutTargetsResponse,
    RoleArn,
    RuleDescription,
    RuleName,
    RuleState,
    ScheduleExpression,
    String,
    TagList,
    TargetList,
    TestEventPatternResponse,
)
from localstack.constants import APPLICATION_AMZ_JSON_1_1
from localstack.http import route
from localstack.services.edge import ROUTER
from localstack.services.events.dispatcher import Event, EventDispatcher
from localstack.services.events.models import EventsStore, events_stores
from localstack.services.events.scheduler import JobId, JobScheduler, parse_schedule_expression
from localstack.services.moto import call_moto
from localstack.services.plugins import ServiceLifecycleHook
from localstack.utils.aws.arns import event_bus_arn, parse_arn
from localstack.utils.aws.client_types import ServicePrincipal
from localstack.utils.aws.message_forwarding import send_event_to_target
from localstack.utils.collections import pick_attributes
from localstack.utils.common import TMP_FILES, mkdir, save_file, truncate
from localstack.utils.json import extract_jsonpath
from localstack.utils.strings import long_uid, short_uid

LOG = logging.getLogger(__name__)

# list of events used to run assertions during integration testing (not exposed to the user)
TEST_EVENTS_CACHE = []
EVENTS_TMP_DIR = "cw_events"
DEFAULT_EVENT_BUS_NAME = "default"
CONTENT_BASE_FILTER_KEYWORDS = ["prefix", "anything-but", "numeric", "cidr", "exists"]
CONNECTION_NAME_PATTERN = re.compile("^[\\.\\-_A-Za-z0-9]+$")


class ValidationException(ServiceException):
    code: str = "ValidationException"
    sender_fault: bool = True
    status_code: int = 400


class EventsProvider(EventsApi, ServiceLifecycleHook):
    def __init__(self):
        apply_patches()
        self.job_scheduler = JobScheduler()

    def on_after_init(self):
        ROUTER.add(self.trigger_scheduled_rule)

    def on_before_start(self):
        self.job_scheduler.start()

    def on_before_stop(self):
        self.job_scheduler.shutdown()

    @route("/_aws/events/rules/<path:rule_arn>/trigger")
    def trigger_scheduled_rule(self, request: Request, rule_arn: str):
        """Developer endpoint to trigger a scheduled rule."""
        arn_data = parse_arn(rule_arn)
        account_id = arn_data["account"]
        region = arn_data["region"]
        rule_name = arn_data["resource"].split("/", maxsplit=1)[-1]

        job_id = events_stores[account_id][region].rule_scheduled_jobs.get(rule_name)
        if not job_id:
            raise NotFound()
        job = self.job_scheduler.get_job(job_id)
        if not job:
            raise NotFound()
        if not job.task:
            raise NotFound(f"Job {job_id} not started")

        job.task.deadline = 0
        self.job_scheduler.scheduler.notify()

    @staticmethod
    def get_store(context: RequestContext) -> EventsStore:
        return events_stores[context.account_id][context.region]

    def test_event_pattern(
        self, context: RequestContext, event_pattern: EventPattern, event: String
    ) -> TestEventPatternResponse:
        # https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_TestEventPattern.html
        # Test event pattern uses event pattern to match against event.
        # So event pattern keys must be in the event keys and values must match.
        # If event pattern has a key that event does not have, it is not a match.
        evt_pattern = json.loads(str(event_pattern))
        evt = json.loads(str(event))

        if any(key not in evt or evt[key] not in values for key, values in evt_pattern.items()):
            return TestEventPatternResponse(Result=False)
        return TestEventPatternResponse(Result=True)

    def put_rule(
        self,
        context: RequestContext,
        name: RuleName,
        schedule_expression: ScheduleExpression = None,
        event_pattern: EventPattern = None,
        state: RuleState = None,
        description: RuleDescription = None,
        role_arn: RoleArn = None,
        tags: TagList = None,
        event_bus_name: EventBusNameOrArn = None,
    ) -> PutRuleResponse:
        response = call_moto(context)

        # rules are defined with either an event_pattern or a schedule_expression. the event_pattern case
        # is currently handled with moto patches, and the scheduled_expression case is handled here
        # explicitly.
        if schedule_expression:
            job_id = self._schedule_rule_job(
                region=context.region,
                account_id=context.account_id,
                event_bus_name_or_arn=event_bus_name,
                rule_name=name,
                rule_state=state,
                schedule_expression=schedule_expression,
            )
            self.get_store(context).rule_scheduled_jobs[name] = job_id

        return response

    def delete_rule(
        self,
        context: RequestContext,
        name: RuleName,
        event_bus_name: EventBusNameOrArn = None,
        force: Boolean = None,
    ) -> None:
        rule_scheduled_jobs = self.get_store(context).rule_scheduled_jobs
        job_id = rule_scheduled_jobs.pop(name, None)
        if job_id:
            LOG.debug("Removing rule: %s (job_id: %s)", name, job_id)
            self.job_scheduler.cancel_job(job_id=job_id)
        call_moto(context)

    def disable_rule(
        self, context: RequestContext, name: RuleName, event_bus_name: EventBusNameOrArn = None
    ) -> None:
        rule_scheduled_jobs = self.get_store(context).rule_scheduled_jobs
        job_id = rule_scheduled_jobs.get(name)
        if job_id:
            LOG.debug("Disabling rule %s (job_id: %s)", name, job_id)
            self.job_scheduler.disable_job(job_id=job_id)
        call_moto(context)

    def enable_rule(
        self, context: RequestContext, name: RuleName, event_bus_name: EventBusNameOrArn = None
    ) -> None:
        rule_scheduled_jobs = self.get_store(context).rule_scheduled_jobs
        job_id = rule_scheduled_jobs.get(name)
        if job_id:
            LOG.debug("Enabling rule %s (job_id: %s)", name, job_id)
            self.job_scheduler.enable_job(job_id=job_id)
        call_moto(context)

    def create_connection(
        self,
        context: RequestContext,
        name: ConnectionName,
        authorization_type: ConnectionAuthorizationType,
        auth_parameters: CreateConnectionAuthRequestParameters,
        description: ConnectionDescription = None,
    ) -> CreateConnectionResponse:
        errors = []

        if not CONNECTION_NAME_PATTERN.match(name):
            error = f"{name} at 'name' failed to satisfy: Member must satisfy regular expression pattern: [\\.\\-_A-Za-z0-9]+"
            errors.append(error)

        if len(name) > 64:
            error = f"{name} at 'name' failed to satisfy: Member must have length less than or equal to 64"
            errors.append(error)

        if authorization_type not in ["BASIC", "API_KEY", "OAUTH_CLIENT_CREDENTIALS"]:
            error = f"{authorization_type} at 'authorizationType' failed to satisfy: Member must satisfy enum value set: [BASIC, OAUTH_CLIENT_CREDENTIALS, API_KEY]"
            errors.append(error)

        if len(errors) > 0:
            error_description = "; ".join(errors)
            error_plural = "errors" if len(errors) > 1 else "error"
            errors_amount = len(errors)
            message = f"{errors_amount} validation {error_plural} detected: {error_description}"
            raise CommonServiceException(message=message, code="ValidationException")

        return call_moto(context)

    def put_targets(
        self,
        context: RequestContext,
        rule: RuleName,
        targets: TargetList,
        event_bus_name: EventBusNameOrArn = None,
    ) -> PutTargetsResponse:
        validation_errors = []

        id_regex = re.compile(r"^[\.\-_A-Za-z0-9]+$")
        for index, target in enumerate(targets):
            id = target.get("Id")
            if not id_regex.match(id):
                validation_errors.append(
                    f"Value '{id}' at 'targets.{index + 1}.member.id' failed to satisfy constraint: Member must satisfy regular expression pattern: [\\.\\-_A-Za-z0-9]+"
                )

            if len(id) > 64:
                validation_errors.append(
                    f"Value '{id}' at 'targets.{index + 1}.member.id' failed to satisfy constraint: Member must have length less than or equal to 64"
                )

        if validation_errors:
            errors_message = "; ".join(validation_errors)
            message = f"{len(validation_errors)} validation {'errors' if len(validation_errors) > 1 else 'error'} detected: {errors_message}"
            raise CommonServiceException(message=message, code="ValidationException")

        return call_moto(context)

    def _schedule_rule_job(
        self,
        region: str,
        account_id: str,
        event_bus_name_or_arn: EventBusNameOrArn,
        rule_name: RuleName,
        rule_state: RuleState,
        schedule_expression: ScheduleExpression,
    ) -> JobId | None:
        """Used when PutRule is used with a ScheduleExpression. It creates a RuleEventDispatcher and
        schedules it using the JobScheduler. Returns the JobId assigned by the JobScheduler."""
        try:
            # guard against invalid expressions
            parse_schedule_expression(schedule_expression)
        except ValueError as e:
            LOG.error("Error parsing schedule expression %s: %s", schedule_expression, e)
            raise ValidationException("Parameter ScheduleExpression is not valid.") from e

        dispatcher = ScheduledRuleDispatcher(
            region, account_id, get_event_bus_name(event_bus_name_or_arn), rule_name
        )

        enabled = rule_state != "DISABLED"
        job_id = self.job_scheduler.add_job(dispatcher, schedule_expression, enabled)
        return job_id


class ScheduledRuleDispatcher:
    """
    Callable used as a Job function for a Rule that was scheduled using a schedule expression.
    """

    def __init__(self, region: str, account_id: str, event_bus_name: str, rule_name: str):
        self.region = region
        self.account_id = account_id
        self.event_bus_name = event_bus_name
        self.rule_name = rule_name

    def __call__(self, *args, **kwargs):
        # look up rule on every call from moto to pick up any updated targets
        from moto.events import events_backends

        moto_backend = events_backends[self.account_id][self.region]
        event_bus = moto_backend.event_buses[self.event_bus_name]
        rule = event_bus.rules.get(self.rule_name)

        if not rule:
            # this should not happen, since the dispatcher is scheduled only after the rule has been
            # created, and is removed once the rule is removed. but we guard against the case anyway.
            LOG.warning(
                "Event rule %s was not found in event bus %s", self.rule_name, self.event_bus_name
            )
            return

        if not rule.targets:
            LOG.debug("Event rule %s was triggered but has no targets", self.rule_name)
            return

        # event id and timestamp should stay the same across targets
        event = Event(
            source="aws.events",
            detail_type="Scheduled Event",
            resources=rule.arn,
            account=self.account_id,
            region=self.region,
        )

        for target in rule.targets:
            # TODO: RetryPolicy
            dispatcher = EventDispatcher.dispatcher_for_target(target.get("Arn"))
            try:
                dispatcher.dispatch(event, target)
            except Exception as e:
                LOG.error(
                    "Failed to dispatch event notification rule %s to %s: %s",
                    rule.name,
                    target,
                    e,
                    exc_info=e if LOG.isEnabledFor(logging.DEBUG) else None,
                )


def _get_events_tmp_dir():
    return os.path.join(config.dirs.tmp, EVENTS_TMP_DIR)


def _create_and_register_temp_dir():
    tmp_dir = _get_events_tmp_dir()
    if not os.path.exists(tmp_dir):
        mkdir(tmp_dir)
        TMP_FILES.append(tmp_dir)
    return tmp_dir


def _dump_events_to_files(events_with_added_uuid):
    try:
        _create_and_register_temp_dir()
        current_time_millis = int(round(time.time() * 1000))
        for event in events_with_added_uuid:
            target = os.path.join(
                _get_events_tmp_dir(),
                "%s_%s" % (current_time_millis, event["uuid"]),
            )
            save_file(target, json.dumps(event["event"]))
    except Exception as e:
        LOG.info("Unable to dump events to tmp dir %s: %s", _get_events_tmp_dir(), e)


def handle_numeric_conditions(conditions: list[Any], value: float):
    for i in range(0, len(conditions), 2):
        if conditions[i] == "<" and not (value < conditions[i + 1]):
            return False
        if conditions[i] == ">" and not (value > conditions[i + 1]):
            return False
        if conditions[i] == "<=" and not (value <= conditions[i + 1]):
            return False
        if conditions[i] == ">=" and not (value >= conditions[i + 1]):
            return False
    return True


def check_valid_numeric_content_base_rule(list_of_operators):
    if len(list_of_operators) > 4:
        return False

    if "=" in list_of_operators:
        return False

    if len(list_of_operators) > 2:
        upper_limit = None
        lower_limit = None
        for index in range(len(list_of_operators)):
            if not isinstance(list_of_operators[index], int) and "<" in list_of_operators[index]:
                upper_limit = list_of_operators[index + 1]
            if not isinstance(list_of_operators[index], int) and ">" in list_of_operators[index]:
                lower_limit = list_of_operators[index + 1]
            if upper_limit and lower_limit and upper_limit < lower_limit:
                return False
            index = index + 1
    return True


def filter_event_with_content_base_parameter(pattern_value: list, event_value: str | int):
    for element in pattern_value:
        if (isinstance(element, (str, int))) and (event_value == element or element in event_value):
            return True
        elif isinstance(element, dict):
            element_key = list(element.keys())[0]
            element_value = element.get(element_key)
            if element_key.lower() == "prefix":
                if isinstance(event_value, str) and event_value.startswith(element_value):
                    return True
            elif element_key.lower() == "exists":
                if element_value and event_value:
                    return True
                elif not element_value and isinstance(event_value, object):
                    return True
            elif element_key.lower() == "cidr":
                ips = [str(ip) for ip in ipaddress.IPv4Network(element_value)]
                if event_value in ips:
                    return True
            elif element_key.lower() == "numeric":
                if check_valid_numeric_content_base_rule(element_value):
                    for index in range(len(element_value)):
                        if isinstance(element_value[index], int):
                            continue
                        if (
                            element_value[index] == ">"
                            and isinstance(element_value[index + 1], int)
                            and event_value <= element_value[index + 1]
                        ):
                            break
                        elif (
                            element_value[index] == ">="
                            and isinstance(element_value[index + 1], int)
                            and event_value < element_value[index + 1]
                        ):
                            break
                        elif (
                            element_value[index] == "<"
                            and isinstance(element_value[index + 1], int)
                            and event_value >= element_value[index + 1]
                        ):
                            break
                        elif (
                            element_value[index] == "<="
                            and isinstance(element_value[index + 1], int)
                            and event_value > element_value[index + 1]
                        ):
                            break
                    else:
                        return True

            elif element_key.lower() == "anything-but":
                if isinstance(element_value, list) and event_value not in element_value:
                    return True
                elif (isinstance(element_value, (str, int))) and event_value != element_value:
                    return True
                elif isinstance(element_value, dict):
                    nested_key = list(element_value)[0]
                    if nested_key == "prefix" and not re.match(
                        r"^{}".format(element_value.get(nested_key)), event_value
                    ):
                        return True
    return False


# TODO: unclear shared responsibility for filtering with filter_event_with_content_base_parameter
def handle_prefix_filtering(event_pattern, value):
    for element in event_pattern:
        if isinstance(element, (int, str)):
            if str(element) == str(value):
                return True
            if element in value:
                return True
        elif isinstance(element, dict) and "prefix" in element:
            if value.startswith(element.get("prefix")):
                return True
        elif isinstance(element, dict) and "anything-but" in element:
            if element.get("anything-but") != value:
                return True
        elif isinstance(element, dict) and "exists" in element:
            if element.get("exists") and value:
                return True
        elif "numeric" in element:
            return handle_numeric_conditions(element.get("numeric"), value)
        elif isinstance(element, list):
            if value in list:
                return True
    return False


def identify_content_base_parameter_in_pattern(parameters) -> bool:
    return any(
        list(param.keys())[0] in CONTENT_BASE_FILTER_KEYWORDS
        for param in parameters
        if isinstance(param, dict)
    )


def get_two_lists_intersection(lst1: List, lst2: List) -> List:
    lst3 = [value for value in lst1 if value in lst2]
    return lst3


def event_pattern_prefix_bool_filter(event_pattern_filter_value_list: list[dict[str, Any]]) -> bool:
    for event_pattern_filter_value in event_pattern_filter_value_list:
        if "exists" in event_pattern_filter_value:
            return event_pattern_filter_value.get("exists")
        else:
            return True


# TODO: refactor/simplify!
def filter_event_based_on_event_format(
    self, rule_name: str, event_bus_name: str, event: dict[str, Any]
):
    def filter_event(event_pattern_filter: dict[str, Any], event: dict[str, Any]):
        for key, value in event_pattern_filter.items():
            fallback = object()
            event_value = event.get(key.lower(), event.get(key, fallback))
            if event_value is fallback and event_pattern_prefix_bool_filter(value):
                return False

            # 1. check if certain values in the event do not match the expected pattern
            if event_value and isinstance(event_value, dict):
                for key_a, value_a in event_value.items():
                    if key_a == "ip":
                        # TODO add IP-Address check here
                        continue
                    if isinstance(value.get(key_a), (int, str)):
                        if value_a != value.get(key_a):
                            return False
                    if isinstance(value.get(key_a), list) and value_a not in value.get(key_a):
                        if not handle_prefix_filtering(value.get(key_a), value_a):
                            return False

            # 2. check if the pattern is a list and event values are not contained in it
            if isinstance(value, list):
                if identify_content_base_parameter_in_pattern(value):
                    if not filter_event_with_content_base_parameter(value, event_value):
                        return False
                else:
                    if (
                        isinstance(event_value, list)
                        and get_two_lists_intersection(value, event_value) == []
                    ):
                        return False
                    if (
                        not isinstance(event_value, list)
                        and isinstance(event_value, (str, int))
                        and event_value not in value
                    ):
                        return False

            # 3. recursively call filter_event(..) for dict types
            elif isinstance(value, (str, dict)):
                try:
                    value = json.loads(value) if isinstance(value, str) else value
                    if isinstance(value, dict) and not filter_event(value, event_value):
                        return False
                except json.decoder.JSONDecodeError:
                    return False

        return True

    rule_information = self.events_backend.describe_rule(
        rule_name, event_bus_arn(event_bus_name, self.current_account, self.region)
    )

    if not rule_information:
        LOG.info('Unable to find rule "%s" in backend: %s', rule_name, rule_information)
        return False
    if rule_information.event_pattern._pattern:
        event_pattern = rule_information.event_pattern._pattern
        if not filter_event(event_pattern, event):
            return False
    return True


def filter_event_with_target_input_path(target: Dict, event: Dict) -> Dict:
    input_path = target.get("InputPath")
    if input_path:
        event = extract_jsonpath(event, input_path)
    return event


def process_events(event: Dict, targets: list[Dict]):
    for target in targets:
        arn = target["Arn"]
        changed_event = filter_event_with_target_input_path(target, event)
        if target.get("Input"):
            changed_event = json.loads(target.get("Input"))
        try:
            send_event_to_target(
                arn,
                changed_event,
                pick_attributes(target, ["$.SqsParameters", "$.KinesisParameters"]),
                role=target.get("RoleArn"),
                target=target,
                source_service=ServicePrincipal.events,
                source_arn=target.get("RuleArn"),
            )
        except Exception as e:
            LOG.info(f"Unable to send event notification {truncate(event)} to target {target}: {e}")


def get_event_bus_name(event_bus_name_or_arn: Optional[EventBusNameOrArn] = None) -> str:
    event_bus_name_or_arn = event_bus_name_or_arn or DEFAULT_EVENT_BUS_NAME
    return event_bus_name_or_arn.split("/")[-1]


# specific logic for put_events which forwards matching events to target listeners
def events_handler_put_events(self):
    # TODO: replace with EventDispatcher
    entries = self._get_param("Entries")

    # keep track of events for local integration testing
    if config.is_local_test_mode():
        TEST_EVENTS_CACHE.extend(entries)

    events = list(map(lambda event: {"event": event, "uuid": str(long_uid())}, entries))

    _dump_events_to_files(events)

    for event_envelope in events:
        event = event_envelope["event"]
        event_bus_name = get_event_bus_name(event.get("EventBusName"))
        event_bus = self.events_backend.event_buses.get(event_bus_name)
        if not event_bus:
            continue

        matching_rules = [
            r
            for r in event_bus.rules.values()
            if r.event_bus_name == event_bus_name and not r.scheduled_expression
        ]
        if not matching_rules:
            continue

        event_time = datetime.datetime.utcnow()
        if event_timestamp := event.get("Time"):
            try:
                # if provided, use the time from event
                event_time = datetime.datetime.utcfromtimestamp(event_timestamp)
            except ValueError:
                # if we can't parse it, pass and keep using `utcnow`
                LOG.debug(
                    "Could not parse the `Time` parameter, falling back to `utcnow` for the following Event: '%s'",
                    event,
                )

        # See https://docs.aws.amazon.com/AmazonS3/latest/userguide/ev-events.html
        formatted_event = {
            "version": "0",
            "id": event_envelope["uuid"],
            "detail-type": event.get("DetailType"),
            "source": event.get("Source"),
            "account": self.current_account,
            "time": event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": self.region,
            "resources": event.get("Resources", []),
            "detail": json.loads(event.get("Detail", "{}")),
        }

        targets = []
        for rule in matching_rules:
            if filter_event_based_on_event_format(self, rule.name, event_bus_name, formatted_event):
                rule_targets = self.events_backend.list_targets_by_rule(
                    rule.name, event_bus_arn(event_bus_name, self.current_account, self.region)
                ).get("Targets", [])

                targets.extend([{"RuleArn": rule.arn} | target for target in rule_targets])

        # process event
        process_events(formatted_event, targets)

    content = {
        "FailedEntryCount": 0,  # TODO: dynamically set proper value when refactoring
        "Entries": list(map(lambda event: {"EventId": event["uuid"]}, events)),
    }

    self.response_headers.update(
        {"Content-Type": APPLICATION_AMZ_JSON_1_1, "x-amzn-RequestId": short_uid()}
    )

    return json.dumps(content), self.response_headers


def apply_patches():
    MotoEventsHandler.put_events = events_handler_put_events
