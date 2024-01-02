# dialects/postgresql/psycopg.py
# Copyright (C) 2005-2024 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: ignore-errors

r"""
.. dialect:: postgresql+psycopg
    :name: psycopg (a.k.a. psycopg 3)
    :dbapi: psycopg
    :connectstring: postgresql+psycopg://user:password@host:port/dbname[?key=value&key=value...]
    :url: https://pypi.org/project/psycopg/

``psycopg`` is the package and module name for version 3 of the ``psycopg``
database driver, formerly known as ``psycopg2``.  This driver is different
enough from its ``psycopg2`` predecessor that SQLAlchemy supports it
via a totally separate dialect; support for ``psycopg2`` is expected to remain
for as long as that package continues to function for modern Python versions,
and also remains the default dialect for the ``postgresql://`` dialect
series.

The SQLAlchemy ``psycopg`` dialect provides both a sync and an async
implementation under the same dialect name. The proper version is
selected depending on how the engine is created:

* calling :func:`_sa.create_engine` with ``postgresql+psycopg://...`` will
  automatically select the sync version, e.g.::

    from sqlalchemy import create_engine
    sync_engine = create_engine("postgresql+psycopg://scott:tiger@localhost/test")

* calling :func:`_asyncio.create_async_engine` with
  ``postgresql+psycopg://...`` will automatically select the async version,
  e.g.::

    from sqlalchemy.ext.asyncio import create_async_engine
    asyncio_engine = create_async_engine("postgresql+psycopg://scott:tiger@localhost/test")

The asyncio version of the dialect may also be specified explicitly using the
``psycopg_async`` suffix, as::

    from sqlalchemy.ext.asyncio import create_async_engine
    asyncio_engine = create_async_engine("postgresql+psycopg_async://scott:tiger@localhost/test")

.. seealso::

    :ref:`postgresql_psycopg2` - The SQLAlchemy ``psycopg``
    dialect shares most of its behavior with the ``psycopg2`` dialect.
    Further documentation is available there.

"""  # noqa
from __future__ import annotations

import collections
import logging
import re
from typing import cast
from typing import TYPE_CHECKING

from . import ranges
from ._psycopg_common import _PGDialect_common_psycopg
from ._psycopg_common import _PGExecutionContext_common_psycopg
from .base import INTERVAL
from .base import PGCompiler
from .base import PGIdentifierPreparer
from .base import REGCONFIG
from .json import JSON
from .json import JSONB
from .json import JSONPathType
from .types import CITEXT
from ... import util
from ...connectors.asyncio import AsyncAdapt_dbapi_connection
from ...connectors.asyncio import AsyncAdapt_dbapi_cursor
from ...connectors.asyncio import AsyncAdapt_dbapi_ss_cursor
from ...sql import sqltypes
from ...util.concurrency import await_

if TYPE_CHECKING:
    from typing import Iterable

logger = logging.getLogger("sqlalchemy.dialects.postgresql")


class _PGString(sqltypes.String):
    render_bind_cast = True


class _PGREGCONFIG(REGCONFIG):
    render_bind_cast = True


class _PGJSON(JSON):
    render_bind_cast = True

    def bind_processor(self, dialect):
        return self._make_bind_processor(None, dialect._psycopg_Json)

    def result_processor(self, dialect, coltype):
        return None


class _PGJSONB(JSONB):
    render_bind_cast = True

    def bind_processor(self, dialect):
        return self._make_bind_processor(None, dialect._psycopg_Jsonb)

    def result_processor(self, dialect, coltype):
        return None


class _PGJSONIntIndexType(sqltypes.JSON.JSONIntIndexType):
    __visit_name__ = "json_int_index"

    render_bind_cast = True


class _PGJSONStrIndexType(sqltypes.JSON.JSONStrIndexType):
    __visit_name__ = "json_str_index"

    render_bind_cast = True


class _PGJSONPathType(JSONPathType):
    pass


class _PGInterval(INTERVAL):
    render_bind_cast = True


class _PGTimeStamp(sqltypes.DateTime):
    render_bind_cast = True


class _PGDate(sqltypes.Date):
    render_bind_cast = True


class _PGTime(sqltypes.Time):
    render_bind_cast = True


class _PGInteger(sqltypes.Integer):
    render_bind_cast = True


class _PGSmallInteger(sqltypes.SmallInteger):
    render_bind_cast = True


class _PGNullType(sqltypes.NullType):
    render_bind_cast = True


class _PGBigInteger(sqltypes.BigInteger):
    render_bind_cast = True


class _PGBoolean(sqltypes.Boolean):
    render_bind_cast = True


class _PsycopgRange(ranges.AbstractRangeImpl):
    def bind_processor(self, dialect):
        psycopg_Range = cast(PGDialect_psycopg, dialect)._psycopg_Range

        def to_range(value):
            if isinstance(value, ranges.Range):
                value = psycopg_Range(
                    value.lower, value.upper, value.bounds, value.empty
                )
            return value

        return to_range

    def result_processor(self, dialect, coltype):
        def to_range(value):
            if value is not None:
                value = ranges.Range(
                    value._lower,
                    value._upper,
                    bounds=value._bounds if value._bounds else "[)",
                    empty=not value._bounds,
                )
            return value

        return to_range


class _PsycopgMultiRange(ranges.AbstractMultiRangeImpl):
    def bind_processor(self, dialect):
        psycopg_Range = cast(PGDialect_psycopg, dialect)._psycopg_Range
        psycopg_Multirange = cast(
            PGDialect_psycopg, dialect
        )._psycopg_Multirange

        NoneType = type(None)

        def to_range(value):
            if isinstance(value, (str, NoneType, psycopg_Multirange)):
                return value

            return psycopg_Multirange(
                [
                    psycopg_Range(
                        element.lower,
                        element.upper,
                        element.bounds,
                        element.empty,
                    )
                    for element in cast("Iterable[ranges.Range]", value)
                ]
            )

        return to_range

    def result_processor(self, dialect, coltype):
        def to_range(value):
            if value is not None:
                value = [
                    ranges.Range(
                        elem._lower,
                        elem._upper,
                        bounds=elem._bounds if elem._bounds else "[)",
                        empty=not elem._bounds,
                    )
                    for elem in value
                ]

            return value

        return to_range


class PGExecutionContext_psycopg(_PGExecutionContext_common_psycopg):
    pass


class PGCompiler_psycopg(PGCompiler):
    pass


class PGIdentifierPreparer_psycopg(PGIdentifierPreparer):
    pass


def _log_notices(diagnostic):
    logger.info("%s: %s", diagnostic.severity, diagnostic.message_primary)


class PGDialect_psycopg(_PGDialect_common_psycopg):
    driver = "psycopg"

    supports_statement_cache = True
    supports_server_side_cursors = True
    default_paramstyle = "pyformat"
    supports_sane_multi_rowcount = True

    execution_ctx_cls = PGExecutionContext_psycopg
    statement_compiler = PGCompiler_psycopg
    preparer = PGIdentifierPreparer_psycopg
    psycopg_version = (0, 0)

    _has_native_hstore = True
    _psycopg_adapters_map = None

    colspecs = util.update_copy(
        _PGDialect_common_psycopg.colspecs,
        {
            sqltypes.String: _PGString,
            REGCONFIG: _PGREGCONFIG,
            JSON: _PGJSON,
            CITEXT: CITEXT,
            sqltypes.JSON: _PGJSON,
            JSONB: _PGJSONB,
            sqltypes.JSON.JSONPathType: _PGJSONPathType,
            sqltypes.JSON.JSONIntIndexType: _PGJSONIntIndexType,
            sqltypes.JSON.JSONStrIndexType: _PGJSONStrIndexType,
            sqltypes.Interval: _PGInterval,
            INTERVAL: _PGInterval,
            sqltypes.Date: _PGDate,
            sqltypes.DateTime: _PGTimeStamp,
            sqltypes.Time: _PGTime,
            sqltypes.Integer: _PGInteger,
            sqltypes.SmallInteger: _PGSmallInteger,
            sqltypes.BigInteger: _PGBigInteger,
            ranges.AbstractRange: _PsycopgRange,
            ranges.AbstractMultiRange: _PsycopgMultiRange,
        },
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        if self.dbapi:
            m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", self.dbapi.__version__)
            if m:
                self.psycopg_version = tuple(
                    int(x) for x in m.group(1, 2, 3) if x is not None
                )

            if self.psycopg_version < (3, 0, 2):
                raise ImportError(
                    "psycopg version 3.0.2 or higher is required."
                )

            from psycopg.adapt import AdaptersMap

            self._psycopg_adapters_map = adapters_map = AdaptersMap(
                self.dbapi.adapters
            )

            if self._native_inet_types is False:
                import psycopg.types.string

                adapters_map.register_loader(
                    "inet", psycopg.types.string.TextLoader
                )
                adapters_map.register_loader(
                    "cidr", psycopg.types.string.TextLoader
                )

            if self._json_deserializer:
                from psycopg.types.json import set_json_loads

                set_json_loads(self._json_deserializer, adapters_map)

            if self._json_serializer:
                from psycopg.types.json import set_json_dumps

                set_json_dumps(self._json_serializer, adapters_map)

    def create_connect_args(self, url):
        # see https://github.com/psycopg/psycopg/issues/83
        cargs, cparams = super().create_connect_args(url)

        if self._psycopg_adapters_map:
            cparams["context"] = self._psycopg_adapters_map
        if self.client_encoding is not None:
            cparams["client_encoding"] = self.client_encoding
        return cargs, cparams

    def _type_info_fetch(self, connection, name):
        from psycopg.types import TypeInfo

        return TypeInfo.fetch(connection.connection.driver_connection, name)

    def initialize(self, connection):
        super().initialize(connection)

        # PGDialect.initialize() checks server version for <= 8.2 and sets
        # this flag to False if so
        if not self.insert_returning:
            self.insert_executemany_returning = False

        # HSTORE can't be registered until we have a connection so that
        # we can look up its OID, so we set up this adapter in
        # initialize()
        if self.use_native_hstore:
            info = self._type_info_fetch(connection, "hstore")
            self._has_native_hstore = info is not None
            if self._has_native_hstore:
                from psycopg.types.hstore import register_hstore

                # register the adapter for connections made subsequent to
                # this one
                register_hstore(info, self._psycopg_adapters_map)

                # register the adapter for this connection
                register_hstore(info, connection.connection)

    @classmethod
    def import_dbapi(cls):
        import psycopg

        return psycopg

    @classmethod
    def get_async_dialect_cls(cls, url):
        return PGDialectAsync_psycopg

    @util.memoized_property
    def _isolation_lookup(self):
        return {
            "READ COMMITTED": self.dbapi.IsolationLevel.READ_COMMITTED,
            "READ UNCOMMITTED": self.dbapi.IsolationLevel.READ_UNCOMMITTED,
            "REPEATABLE READ": self.dbapi.IsolationLevel.REPEATABLE_READ,
            "SERIALIZABLE": self.dbapi.IsolationLevel.SERIALIZABLE,
        }

    @util.memoized_property
    def _psycopg_Json(self):
        from psycopg.types import json

        return json.Json

    @util.memoized_property
    def _psycopg_Jsonb(self):
        from psycopg.types import json

        return json.Jsonb

    @util.memoized_property
    def _psycopg_TransactionStatus(self):
        from psycopg.pq import TransactionStatus

        return TransactionStatus

    @util.memoized_property
    def _psycopg_Range(self):
        from psycopg.types.range import Range

        return Range

    @util.memoized_property
    def _psycopg_Multirange(self):
        from psycopg.types.multirange import Multirange

        return Multirange

    def _do_isolation_level(self, connection, autocommit, isolation_level):
        connection.autocommit = autocommit
        connection.isolation_level = isolation_level

    def get_isolation_level(self, dbapi_connection):
        status_before = dbapi_connection.info.transaction_status
        value = super().get_isolation_level(dbapi_connection)

        # don't rely on psycopg providing enum symbols, compare with
        # eq/ne
        if status_before == self._psycopg_TransactionStatus.IDLE:
            dbapi_connection.rollback()
        return value

    def set_isolation_level(self, dbapi_connection, level):
        if level == "AUTOCOMMIT":
            self._do_isolation_level(
                dbapi_connection, autocommit=True, isolation_level=None
            )
        else:
            self._do_isolation_level(
                dbapi_connection,
                autocommit=False,
                isolation_level=self._isolation_lookup[level],
            )

    def set_readonly(self, connection, value):
        connection.read_only = value

    def get_readonly(self, connection):
        return connection.read_only

    def on_connect(self):
        def notices(conn):
            conn.add_notice_handler(_log_notices)

        fns = [notices]

        if self.isolation_level is not None:

            def on_connect(conn):
                self.set_isolation_level(conn, self.isolation_level)

            fns.append(on_connect)

        # fns always has the notices function
        def on_connect(conn):
            for fn in fns:
                fn(conn)

        return on_connect

    def is_disconnect(self, e, connection, cursor):
        if isinstance(e, self.dbapi.Error) and connection is not None:
            if connection.closed or connection.broken:
                return True
        return False

    def _do_prepared_twophase(self, connection, command, recover=False):
        dbapi_conn = connection.connection.dbapi_connection
        if (
            recover
            # don't rely on psycopg providing enum symbols, compare with
            # eq/ne
            or dbapi_conn.info.transaction_status
            != self._psycopg_TransactionStatus.IDLE
        ):
            dbapi_conn.rollback()
        before_autocommit = dbapi_conn.autocommit
        try:
            if not before_autocommit:
                self._do_autocommit(dbapi_conn, True)
            with dbapi_conn.cursor() as cursor:
                cursor.execute(command)
        finally:
            if not before_autocommit:
                self._do_autocommit(dbapi_conn, before_autocommit)

    def do_rollback_twophase(
        self, connection, xid, is_prepared=True, recover=False
    ):
        if is_prepared:
            self._do_prepared_twophase(
                connection, f"ROLLBACK PREPARED '{xid}'", recover=recover
            )
        else:
            self.do_rollback(connection.connection)

    def do_commit_twophase(
        self, connection, xid, is_prepared=True, recover=False
    ):
        if is_prepared:
            self._do_prepared_twophase(
                connection, f"COMMIT PREPARED '{xid}'", recover=recover
            )
        else:
            self.do_commit(connection.connection)

    @util.memoized_property
    def _dialect_specific_select_one(self):
        return ";"


class AsyncAdapt_psycopg_cursor(AsyncAdapt_dbapi_cursor):
    __slots__ = ()

    def close(self):
        self._rows.clear()
        # Normal cursor just call _close() in a non-sync way.
        self._cursor._close()

    async def _execute_async(self, operation, parameters):
        # override to not use mutex, psycopg3 already has mutex

        if parameters is None:
            result = await self._cursor.execute(operation)
        else:
            result = await self._cursor.execute(operation, parameters)

        # sqlalchemy result is not async, so need to pull all rows here
        # (assuming not a server side cursor)
        res = self._cursor.pgresult

        # don't rely on psycopg providing enum symbols, compare with
        # eq/ne
        if (
            not self.server_side
            and res
            and res.status == self._adapt_connection.dbapi.ExecStatus.TUPLES_OK
        ):
            self._rows = collections.deque(await self._cursor.fetchall())
        return result

    async def _executemany_async(
        self,
        operation,
        seq_of_parameters,
    ):
        # override to not use mutex, psycopg3 already has mutex
        return await self._cursor.executemany(operation, seq_of_parameters)


class AsyncAdapt_psycopg_ss_cursor(
    AsyncAdapt_dbapi_ss_cursor, AsyncAdapt_psycopg_cursor
):
    __slots__ = ("name",)

    name: str

    def __init__(self, adapt_connection, name):
        self.name = name
        super().__init__(adapt_connection)

    def _make_new_cursor(self, connection):
        return connection.cursor(self.name)

    # TODO: should this be on the base asyncio adapter?
    def __iter__(self):
        iterator = self._cursor.__aiter__()
        while True:
            try:
                yield await_(iterator.__anext__())
            except StopAsyncIteration:
                break


class AsyncAdapt_psycopg_connection(AsyncAdapt_dbapi_connection):
    __slots__ = ()

    _cursor_cls = AsyncAdapt_psycopg_cursor
    _ss_cursor_cls = AsyncAdapt_psycopg_ss_cursor

    def add_notice_handler(self, handler):
        self._connection.add_notice_handler(handler)

    @property
    def info(self):
        return self._connection.info

    @property
    def adapters(self):
        return self._connection.adapters

    @property
    def closed(self):
        return self._connection.closed

    @property
    def broken(self):
        return self._connection.broken

    @property
    def read_only(self):
        return self._connection.read_only

    @property
    def deferrable(self):
        return self._connection.deferrable

    @property
    def autocommit(self):
        return self._connection.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.set_autocommit(value)

    def set_autocommit(self, value):
        await_(self._connection.set_autocommit(value))

    def set_isolation_level(self, value):
        await_(self._connection.set_isolation_level(value))

    def set_read_only(self, value):
        await_(self._connection.set_read_only(value))

    def set_deferrable(self, value):
        await_(self._connection.set_deferrable(value))

    def cursor(self, name=None, /):
        if name:
            return AsyncAdapt_psycopg_ss_cursor(self, name)
        else:
            return AsyncAdapt_psycopg_cursor(self)


class PsycopgAdaptDBAPI:
    def __init__(self, psycopg, ExecStatus) -> None:
        self.psycopg = psycopg
        self.ExecStatus = ExecStatus

        for k, v in self.psycopg.__dict__.items():
            if k != "connect":
                self.__dict__[k] = v

    def connect(self, *arg, **kw):
        creator_fn = kw.pop(
            "async_creator_fn", self.psycopg.AsyncConnection.connect
        )
        return AsyncAdapt_psycopg_connection(
            self, await_(creator_fn(*arg, **kw))
        )


class PGDialectAsync_psycopg(PGDialect_psycopg):
    is_async = True
    supports_statement_cache = True

    @classmethod
    def import_dbapi(cls):
        import psycopg
        from psycopg.pq import ExecStatus

        return PsycopgAdaptDBAPI(psycopg, ExecStatus)

    def _type_info_fetch(self, connection, name):
        from psycopg.types import TypeInfo

        adapted = connection.connection
        return await_(TypeInfo.fetch(adapted.driver_connection, name))

    def _do_isolation_level(self, connection, autocommit, isolation_level):
        connection.set_autocommit(autocommit)
        connection.set_isolation_level(isolation_level)

    def _do_autocommit(self, connection, value):
        connection.set_autocommit(value)

    def set_readonly(self, connection, value):
        connection.set_read_only(value)

    def set_deferrable(self, connection, value):
        connection.set_deferrable(value)

    def get_driver_connection(self, connection):
        return connection._connection


dialect = PGDialect_psycopg
dialect_async = PGDialectAsync_psycopg
