"""Models for SQLAlchemy."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time
from typing import Any, TypeVar, cast

import ciso8601
from fnvhash import fnv1a_32
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    distinct,
    type_coerce,
)
from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import aliased, declarative_base, relationship
from sqlalchemy.orm.session import Session

from homeassistant.const import (
    MAX_LENGTH_EVENT_CONTEXT_ID,
    MAX_LENGTH_EVENT_EVENT_TYPE,
    MAX_LENGTH_EVENT_ORIGIN,
    MAX_LENGTH_STATE_ENTITY_ID,
    MAX_LENGTH_STATE_STATE,
)
from homeassistant.core import Context, Event, EventOrigin, State, split_entity_id
from homeassistant.helpers.json import (
    JSON_DECODE_EXCEPTIONS,
    JSON_DUMP,
    json_bytes,
    json_bytes_strip_null,
    json_loads,
)
import homeassistant.util.dt as dt_util

from .const import ALL_DOMAIN_EXCLUDE_ATTRS, SupportedDialect
from .models import StatisticData, StatisticMetaData, process_timestamp

# SQLAlchemy Schema
# pylint: disable=invalid-name
Base = declarative_base()

SCHEMA_VERSION = 33

_StatisticsBaseSelfT = TypeVar("_StatisticsBaseSelfT", bound="StatisticsBase")

_LOGGER = logging.getLogger(__name__)

TABLE_EVENTS = "events"
TABLE_EVENT_DATA = "event_data"
TABLE_STATES = "states"
TABLE_STATE_ATTRIBUTES = "state_attributes"
TABLE_RECORDER_RUNS = "recorder_runs"
TABLE_SCHEMA_CHANGES = "schema_changes"
TABLE_STATISTICS = "statistics"
TABLE_STATISTICS_META = "statistics_meta"
TABLE_STATISTICS_RUNS = "statistics_runs"
TABLE_STATISTICS_SHORT_TERM = "statistics_short_term"

MAX_STATE_ATTRS_BYTES = 16384
PSQL_DIALECT = SupportedDialect.POSTGRESQL

ALL_TABLES = [
    TABLE_STATES,
    TABLE_STATE_ATTRIBUTES,
    TABLE_EVENTS,
    TABLE_EVENT_DATA,
    TABLE_RECORDER_RUNS,
    TABLE_SCHEMA_CHANGES,
    TABLE_STATISTICS,
    TABLE_STATISTICS_META,
    TABLE_STATISTICS_RUNS,
    TABLE_STATISTICS_SHORT_TERM,
]

TABLES_TO_CHECK = [
    TABLE_STATES,
    TABLE_EVENTS,
    TABLE_RECORDER_RUNS,
    TABLE_SCHEMA_CHANGES,
]

LAST_UPDATED_INDEX_TS = "ix_states_last_updated_ts"
ENTITY_ID_LAST_UPDATED_INDEX_TS = "ix_states_entity_id_last_updated_ts"
EVENTS_CONTEXT_ID_INDEX = "ix_events_context_id"
STATES_CONTEXT_ID_INDEX = "ix_states_context_id"


class FAST_PYSQLITE_DATETIME(sqlite.DATETIME):  # type: ignore[misc]
    """Use ciso8601 to parse datetimes instead of sqlalchemy built-in regex."""

    def result_processor(self, dialect, coltype):  # type: ignore[no-untyped-def]
        """Offload the datetime parsing to ciso8601."""
        return lambda value: None if value is None else ciso8601.parse_datetime(value)


JSON_VARIANT_CAST = Text().with_variant(
    postgresql.JSON(none_as_null=True), "postgresql"
)
JSONB_VARIANT_CAST = Text().with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)
DATETIME_TYPE = (
    DateTime(timezone=True)
    .with_variant(mysql.DATETIME(timezone=True, fsp=6), "mysql")
    .with_variant(FAST_PYSQLITE_DATETIME(), "sqlite")
)
DOUBLE_TYPE = (
    Float()
    .with_variant(mysql.DOUBLE(asdecimal=False), "mysql")
    .with_variant(oracle.DOUBLE_PRECISION(), "oracle")
    .with_variant(postgresql.DOUBLE_PRECISION(), "postgresql")
)

TIMESTAMP_TYPE = DOUBLE_TYPE


class JSONLiteral(JSON):  # type: ignore[misc]
    """Teach SA how to literalize json."""

    def literal_processor(self, dialect: str) -> Callable[[Any], str]:
        """Processor to convert a value to JSON."""

        def process(value: Any) -> str:
            """Dump json."""
            return JSON_DUMP(value)

        return process


EVENT_ORIGIN_ORDER = [EventOrigin.local, EventOrigin.remote]
EVENT_ORIGIN_TO_IDX = {origin: idx for idx, origin in enumerate(EVENT_ORIGIN_ORDER)}


class Events(Base):  # type: ignore[misc,valid-type]
    """Event history data."""

    __table_args__ = (
        # Used for fetching events at a specific time
        # see logbook
        Index("ix_events_event_type_time_fired_ts", "event_type", "time_fired_ts"),
        {"mysql_default_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
    __tablename__ = TABLE_EVENTS
    event_id = Column(Integer, Identity(), primary_key=True)
    event_type = Column(String(MAX_LENGTH_EVENT_EVENT_TYPE))
    event_data = Column(Text().with_variant(mysql.LONGTEXT, "mysql"))
    origin = Column(String(MAX_LENGTH_EVENT_ORIGIN))  # no longer used for new rows
    origin_idx = Column(SmallInteger)
    time_fired = Column(DATETIME_TYPE)  # no longer used for new rows
    time_fired_ts = Column(TIMESTAMP_TYPE, index=True)
    context_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID), index=True)
    context_user_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID))
    context_parent_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID))
    data_id = Column(Integer, ForeignKey("event_data.data_id"), index=True)
    event_data_rel = relationship("EventData")

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            "<recorder.Events("
            f"id={self.event_id}, type='{self.event_type}', "
            f"origin_idx='{self.origin_idx}', time_fired='{self._time_fired_isotime}'"
            f", data_id={self.data_id})>"
        )

    @property
    def _time_fired_isotime(self) -> str:
        """Return time_fired as an isotime string."""
        if self.time_fired_ts is not None:
            date_time = dt_util.utc_from_timestamp(self.time_fired_ts)
        else:
            date_time = process_timestamp(self.time_fired)
        return date_time.isoformat(sep=" ", timespec="seconds")

    @staticmethod
    def from_event(event: Event) -> Events:
        """Create an event database object from a native event."""
        return Events(
            event_type=event.event_type,
            event_data=None,
            origin_idx=EVENT_ORIGIN_TO_IDX.get(event.origin),
            time_fired=None,
            time_fired_ts=dt_util.utc_to_timestamp(event.time_fired),
            context_id=event.context.id,
            context_user_id=event.context.user_id,
            context_parent_id=event.context.parent_id,
        )

    def to_native(self, validate_entity_id: bool = True) -> Event | None:
        """Convert to a native HA Event."""
        context = Context(
            id=self.context_id,
            user_id=self.context_user_id,
            parent_id=self.context_parent_id,
        )
        try:
            return Event(
                self.event_type,
                json_loads(self.event_data) if self.event_data else {},
                EventOrigin(self.origin)
                if self.origin
                else EVENT_ORIGIN_ORDER[self.origin_idx],
                dt_util.utc_from_timestamp(self.time_fired_ts),
                context=context,
            )
        except JSON_DECODE_EXCEPTIONS:
            # When json_loads fails
            _LOGGER.exception("Error converting to event: %s", self)
            return None


class EventData(Base):  # type: ignore[misc,valid-type]
    """Event data history."""

    __table_args__ = (
        {"mysql_default_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
    __tablename__ = TABLE_EVENT_DATA
    data_id = Column(Integer, Identity(), primary_key=True)
    hash = Column(BigInteger, index=True)
    # Note that this is not named attributes to avoid confusion with the states table
    shared_data = Column(Text().with_variant(mysql.LONGTEXT, "mysql"))

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            "<recorder.EventData("
            f"id={self.data_id}, hash='{self.hash}', data='{self.shared_data}'"
            ")>"
        )

    @staticmethod
    def shared_data_bytes_from_event(
        event: Event, dialect: SupportedDialect | None
    ) -> bytes:
        """Create shared_data from an event."""
        if dialect == SupportedDialect.POSTGRESQL:
            return json_bytes_strip_null(event.data)
        return json_bytes(event.data)

    @staticmethod
    def hash_shared_data_bytes(shared_data_bytes: bytes) -> int:
        """Return the hash of json encoded shared data."""
        return cast(int, fnv1a_32(shared_data_bytes))

    def to_native(self) -> dict[str, Any]:
        """Convert to an HA state object."""
        try:
            return cast(dict[str, Any], json_loads(self.shared_data))
        except JSON_DECODE_EXCEPTIONS:
            _LOGGER.exception("Error converting row to event data: %s", self)
            return {}


class States(Base):  # type: ignore[misc,valid-type]
    """State change history."""

    __table_args__ = (
        # Used for fetching the state of entities at a specific time
        # (get_states in history.py)
        Index(ENTITY_ID_LAST_UPDATED_INDEX_TS, "entity_id", "last_updated_ts"),
        {"mysql_default_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
    __tablename__ = TABLE_STATES
    state_id = Column(Integer, Identity(), primary_key=True)
    entity_id = Column(String(MAX_LENGTH_STATE_ENTITY_ID))
    state = Column(String(MAX_LENGTH_STATE_STATE))
    attributes = Column(
        Text().with_variant(mysql.LONGTEXT, "mysql")
    )  # no longer used for new rows
    event_id = Column(  # no longer used for new rows
        Integer, ForeignKey("events.event_id", ondelete="CASCADE"), index=True
    )
    last_changed = Column(DATETIME_TYPE)  # no longer used for new rows
    last_changed_ts = Column(TIMESTAMP_TYPE)
    last_updated = Column(DATETIME_TYPE)  # no longer used for new rows
    last_updated_ts = Column(TIMESTAMP_TYPE, default=time.time, index=True)
    old_state_id = Column(Integer, ForeignKey("states.state_id"), index=True)
    attributes_id = Column(
        Integer, ForeignKey("state_attributes.attributes_id"), index=True
    )
    context_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID), index=True)
    context_user_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID))
    context_parent_id = Column(String(MAX_LENGTH_EVENT_CONTEXT_ID))
    origin_idx = Column(SmallInteger)  # 0 is local, 1 is remote
    old_state = relationship("States", remote_side=[state_id])
    state_attributes = relationship("StateAttributes")

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            f"<recorder.States(id={self.state_id}, entity_id='{self.entity_id}',"
            f" state='{self.state}', event_id='{self.event_id}',"
            f" last_updated='{self._last_updated_isotime}',"
            f" old_state_id={self.old_state_id}, attributes_id={self.attributes_id})>"
        )

    @property
    def _last_updated_isotime(self) -> str:
        """Return last_updated as an isotime string."""
        if self.last_updated_ts is not None:
            date_time = dt_util.utc_from_timestamp(self.last_updated_ts)
        else:
            date_time = process_timestamp(self.last_updated)
        return date_time.isoformat(sep=" ", timespec="seconds")

    @staticmethod
    def from_event(event: Event) -> States:
        """Create object from a state_changed event."""
        entity_id = event.data["entity_id"]
        state: State | None = event.data.get("new_state")
        dbstate = States(
            entity_id=entity_id,
            attributes=None,
            context_id=event.context.id,
            context_user_id=event.context.user_id,
            context_parent_id=event.context.parent_id,
            origin_idx=EVENT_ORIGIN_TO_IDX.get(event.origin),
            last_updated=None,
            last_changed=None,
        )
        # None state means the state was removed from the state machine
        if state is None:
            dbstate.state = ""
            dbstate.last_updated_ts = dt_util.utc_to_timestamp(event.time_fired)
            dbstate.last_changed_ts = None
            return dbstate

        dbstate.state = state.state
        dbstate.last_updated_ts = dt_util.utc_to_timestamp(state.last_updated)
        if state.last_updated == state.last_changed:
            dbstate.last_changed_ts = None
        else:
            dbstate.last_changed_ts = dt_util.utc_to_timestamp(state.last_changed)

        return dbstate

    def to_native(self, validate_entity_id: bool = True) -> State | None:
        """Convert to an HA state object."""
        context = Context(
            id=self.context_id,
            user_id=self.context_user_id,
            parent_id=self.context_parent_id,
        )
        try:
            attrs = json_loads(self.attributes) if self.attributes else {}
        except JSON_DECODE_EXCEPTIONS:
            # When json_loads fails
            _LOGGER.exception("Error converting row to state: %s", self)
            return None
        if self.last_changed_ts is None or self.last_changed_ts == self.last_updated_ts:
            last_changed = last_updated = dt_util.utc_from_timestamp(
                self.last_updated_ts or 0
            )
        else:
            last_updated = dt_util.utc_from_timestamp(self.last_updated_ts or 0)
            last_changed = dt_util.utc_from_timestamp(self.last_changed_ts or 0)
        return State(
            self.entity_id,
            self.state,
            # Join the state_attributes table on attributes_id to get the attributes
            # for newer states
            attrs,
            last_changed,
            last_updated,
            context=context,
            validate_entity_id=validate_entity_id,
        )


class StateAttributes(Base):  # type: ignore[misc,valid-type]
    """State attribute change history."""

    __table_args__ = (
        {"mysql_default_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
    __tablename__ = TABLE_STATE_ATTRIBUTES
    attributes_id = Column(Integer, Identity(), primary_key=True)
    hash = Column(BigInteger, index=True)
    # Note that this is not named attributes to avoid confusion with the states table
    shared_attrs = Column(Text().with_variant(mysql.LONGTEXT, "mysql"))

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            f"<recorder.StateAttributes(id={self.attributes_id}, hash='{self.hash}',"
            f" attributes='{self.shared_attrs}')>"
        )

    @staticmethod
    def shared_attrs_bytes_from_event(
        event: Event,
        exclude_attrs_by_domain: dict[str, set[str]],
        dialect: SupportedDialect | None,
    ) -> bytes:
        """Create shared_attrs from a state_changed event."""
        state: State | None = event.data.get("new_state")
        # None state means the state was removed from the state machine
        if state is None:
            return b"{}"
        domain = split_entity_id(state.entity_id)[0]
        exclude_attrs = (
            exclude_attrs_by_domain.get(domain, set()) | ALL_DOMAIN_EXCLUDE_ATTRS
        )
        encoder = json_bytes_strip_null if dialect == PSQL_DIALECT else json_bytes
        bytes_result = encoder(
            {k: v for k, v in state.attributes.items() if k not in exclude_attrs}
        )
        if len(bytes_result) > MAX_STATE_ATTRS_BYTES:
            _LOGGER.warning(
                "State attributes for %s exceed maximum size of %s bytes. "
                "This can cause database performance issues; Attributes "
                "will not be stored",
                state.entity_id,
                MAX_STATE_ATTRS_BYTES,
            )
            return b"{}"
        return bytes_result

    @staticmethod
    def hash_shared_attrs_bytes(shared_attrs_bytes: bytes) -> int:
        """Return the hash of json encoded shared attributes."""
        return cast(int, fnv1a_32(shared_attrs_bytes))

    def to_native(self) -> dict[str, Any]:
        """Convert to an HA state object."""
        try:
            return cast(dict[str, Any], json_loads(self.shared_attrs))
        except JSON_DECODE_EXCEPTIONS:
            # When json_loads fails
            _LOGGER.exception("Error converting row to state attributes: %s", self)
            return {}


class StatisticsBase:
    """Statistics base class."""

    id = Column(Integer, Identity(), primary_key=True)
    created = Column(DATETIME_TYPE, default=dt_util.utcnow)

    @declared_attr  # type: ignore[misc]
    def metadata_id(self) -> Column:
        """Define the metadata_id column for sub classes."""
        return Column(
            Integer,
            ForeignKey(f"{TABLE_STATISTICS_META}.id", ondelete="CASCADE"),
            index=True,
        )

    start = Column(DATETIME_TYPE, index=True)
    mean = Column(DOUBLE_TYPE)
    min = Column(DOUBLE_TYPE)
    max = Column(DOUBLE_TYPE)
    last_reset = Column(DATETIME_TYPE)
    state = Column(DOUBLE_TYPE)
    sum = Column(DOUBLE_TYPE)

    @classmethod
    def from_stats(
        cls: type[_StatisticsBaseSelfT], metadata_id: int, stats: StatisticData
    ) -> _StatisticsBaseSelfT:
        """Create object from a statistics."""
        return cls(  # type: ignore[call-arg,misc]
            metadata_id=metadata_id,
            **stats,
        )


class Statistics(Base, StatisticsBase):  # type: ignore[misc,valid-type]
    """Long term statistics."""

    duration = timedelta(hours=1)

    __table_args__ = (
        # Used for fetching statistics for a certain entity at a specific time
        Index("ix_statistics_statistic_id_start", "metadata_id", "start", unique=True),
    )
    __tablename__ = TABLE_STATISTICS


class StatisticsShortTerm(Base, StatisticsBase):  # type: ignore[misc,valid-type]
    """Short term statistics."""

    duration = timedelta(minutes=5)

    __table_args__ = (
        # Used for fetching statistics for a certain entity at a specific time
        Index(
            "ix_statistics_short_term_statistic_id_start",
            "metadata_id",
            "start",
            unique=True,
        ),
    )
    __tablename__ = TABLE_STATISTICS_SHORT_TERM


class StatisticsMeta(Base):  # type: ignore[misc,valid-type]
    """Statistics meta data."""

    __table_args__ = (
        {"mysql_default_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
    __tablename__ = TABLE_STATISTICS_META
    id = Column(Integer, Identity(), primary_key=True)
    statistic_id = Column(String(255), index=True, unique=True)
    source = Column(String(32))
    unit_of_measurement = Column(String(255))
    has_mean = Column(Boolean)
    has_sum = Column(Boolean)
    name = Column(String(255))

    @staticmethod
    def from_meta(meta: StatisticMetaData) -> StatisticsMeta:
        """Create object from meta data."""
        return StatisticsMeta(**meta)


class RecorderRuns(Base):  # type: ignore[misc,valid-type]
    """Representation of recorder run."""

    __table_args__ = (Index("ix_recorder_runs_start_end", "start", "end"),)
    __tablename__ = TABLE_RECORDER_RUNS
    run_id = Column(Integer, Identity(), primary_key=True)
    start = Column(DATETIME_TYPE, default=dt_util.utcnow)
    end = Column(DATETIME_TYPE)
    closed_incorrect = Column(Boolean, default=False)
    created = Column(DATETIME_TYPE, default=dt_util.utcnow)

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        end = (
            f"'{self.end.isoformat(sep=' ', timespec='seconds')}'" if self.end else None
        )
        return (
            f"<recorder.RecorderRuns(id={self.run_id},"
            f" start='{self.start.isoformat(sep=' ', timespec='seconds')}', end={end},"
            f" closed_incorrect={self.closed_incorrect},"
            f" created='{self.created.isoformat(sep=' ', timespec='seconds')}')>"
        )

    def entity_ids(self, point_in_time: datetime | None = None) -> list[str]:
        """Return the entity ids that existed in this run.

        Specify point_in_time if you want to know which existed at that point
        in time inside the run.
        """
        session = Session.object_session(self)

        assert session is not None, "RecorderRuns need to be persisted"

        query = session.query(distinct(States.entity_id)).filter(
            States.last_updated >= self.start
        )

        if point_in_time is not None:
            query = query.filter(States.last_updated < point_in_time)
        elif self.end is not None:
            query = query.filter(States.last_updated < self.end)

        return [row[0] for row in query]

    def to_native(self, validate_entity_id: bool = True) -> RecorderRuns:
        """Return self, native format is this model."""
        return self


class SchemaChanges(Base):  # type: ignore[misc,valid-type]
    """Representation of schema version changes."""

    __tablename__ = TABLE_SCHEMA_CHANGES
    change_id = Column(Integer, Identity(), primary_key=True)
    schema_version = Column(Integer)
    changed = Column(DATETIME_TYPE, default=dt_util.utcnow)

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            "<recorder.SchemaChanges("
            f"id={self.change_id}, schema_version={self.schema_version}, "
            f"changed='{self.changed.isoformat(sep=' ', timespec='seconds')}'"
            ")>"
        )


class StatisticsRuns(Base):  # type: ignore[misc,valid-type]
    """Representation of statistics run."""

    __tablename__ = TABLE_STATISTICS_RUNS
    run_id = Column(Integer, Identity(), primary_key=True)
    start = Column(DATETIME_TYPE, index=True)

    def __repr__(self) -> str:
        """Return string representation of instance for debugging."""
        return (
            f"<recorder.StatisticsRuns(id={self.run_id},"
            f" start='{self.start.isoformat(sep=' ', timespec='seconds')}', )>"
        )


EVENT_DATA_JSON = type_coerce(
    EventData.shared_data.cast(JSONB_VARIANT_CAST), JSONLiteral(none_as_null=True)
)
OLD_FORMAT_EVENT_DATA_JSON = type_coerce(
    Events.event_data.cast(JSONB_VARIANT_CAST), JSONLiteral(none_as_null=True)
)

SHARED_ATTRS_JSON = type_coerce(
    StateAttributes.shared_attrs.cast(JSON_VARIANT_CAST), JSON(none_as_null=True)
)
OLD_FORMAT_ATTRS_JSON = type_coerce(
    States.attributes.cast(JSON_VARIANT_CAST), JSON(none_as_null=True)
)

ENTITY_ID_IN_EVENT: Column = EVENT_DATA_JSON["entity_id"]
OLD_ENTITY_ID_IN_EVENT: Column = OLD_FORMAT_EVENT_DATA_JSON["entity_id"]
DEVICE_ID_IN_EVENT: Column = EVENT_DATA_JSON["device_id"]
OLD_STATE = aliased(States, name="old_state")
