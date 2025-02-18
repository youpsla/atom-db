"""
Copyright (c) 2018-2020, Jairus Martin.

Distributed under the terms of the MIT License.

The full license is in the file LICENSE.txt, distributed with this software.

Created on Aug 2, 2018

@author: jrm
"""
import os
import logging
import datetime
import weakref
import asyncio
import sqlalchemy as sa
from decimal import Decimal
from functools import wraps
from typing import Dict as DictType
from typing import List as ListType
from typing import Tuple as TupleType
from typing import Set as SetType
from typing import Callable as CallableType
from typing import (
    Any,
    ClassVar,
    Type,
    Optional,
    Iterator,
    Union,
    Sequence,
    Generic,
    TypeVar,
    cast,
)
from atom import api
from atom.api import (
    Atom,
    Member,
    Subclass,
    ContainerList,
    Int,
    Dict,
    Instance,
    Typed,
    Property,
    Str,
    ForwardInstance,
    ForwardSubclass,
    Value,
    Bool,
    List,
)
from sqlalchemy.engine import ddl, strategies
from sqlalchemy.sql import schema
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy import func
from .base import (
    ModelManager,
    ModelSerializer,
    Model,
    ModelMeta,
    JSONModel,
    JSONSerializer,
    ScopeType,
    StateType,
    find_subclasses,
)

# kwargs reserved for sqlalchemy table columns
COLUMN_KWARGS = (
    "autoincrement",
    "default",
    "doc",
    "key",
    "index",
    "info",
    "nullable",
    "onupdate",
    "primary_key",
    "server_default",
    "server_onupdate",
    "quote",
    "unique",
    "system",
    "comment",
)
FK_TYPES = (api.Instance, api.Typed, api.ForwardInstance, api.ForwardTyped)

# ops that can be used with django-style queries
QUERY_OPS = {
    "eq": "__eq__",
    "gt": "__gt__",
    "gte": "__ge__",
    "ge": "__ge__",
    "lt": "__lt__",
    "le": "__le__",
    "lte": "__le__",
    "all": "all_",
    "any": "any_",
    "ne": "__ne__",
    "not": "__ne__",
    "contains": "contains",
    "endswith": "endswith",
    "ilike": "ilike",
    "in": "in_",
    "is": "is_",
    "is_distinct_from": "is_distinct_from",
    "isnot": "isnot",
    "isnot_distinct_from": "isnot_distinct_from",
    "like": "like",
    "match": "match",
    "notilike": "notilike",
    "notlike": "notlike",
    "notin": "notin_",
    "startswith": "startswith",
}

# Fields supported on the django style Meta class of a model
VALID_META_FIELDS = (
    "db_name",
    "db_table",
    "unique_together",
    "abstract",
    "constraints",
    "triggers",
    "composite_indexes",
)

# Constraint naming conventions
CONSTRAINT_NAMING_CONVENTIONS = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    # Using "ck_%(table_name)s_%(constraint_name)s" is preferred but it causes
    # issues using Bool on mysql
    "ck": "ck_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

log = logging.getLogger("atomdb.sql")

QueryType = Union[str, sa.sql.expression.Executable]
T = TypeVar("T", bound="SQLModel")


def find_sql_models() -> Iterator[Type["SQLModel"]]:
    """Finds all non-abstract imported SQLModels by looking up subclasses
    of the SQLModel.

    Yields
    ------
    cls: SQLModel

    """
    for model in find_subclasses(SQLModel):
        # Get model Meta class
        meta = getattr(model, "Meta", None)
        if meta:
            # If this is marked as abstract ignore it
            if getattr(meta, "abstract", False):
                continue
        yield model


class Relation(ContainerList):
    """A member which serves as a fk relation backref"""

    __slots__ = ("_to",)

    def __init__(self, item: CallableType[[], Type[Model]], default=None):
        super().__init__(ForwardInstance(item), default=default)  # type: ignore
        self._to: Optional[Type[Model]] = None

    def resolve(self) -> Type[Model]:
        return self.to

    @property
    def to(self) -> Type[Model]:
        to = self._to
        if to is None:
            types = resolve_member_types(self.validate_mode[-1])
            assert types is not None
            to = self._to = types[0]
        return to


def py_type_to_sql_column(
    model: Type[Model],
    member: Member,
    types: Union[Type, TupleType[Type, ...]],
    **kwargs,
) -> TypeEngine:
    """Convert the python type to an alchemy table column type"""
    if isinstance(types, tuple):
        cls, *subtypes = types
    else:
        cls = types

    if issubclass(cls, JSONModel):
        return sa.JSON(**kwargs)
    elif issubclass(cls, SQLModel):
        name = f"{cls.__model__}.{cls.__pk__}"
        cls.__backrefs__.add((model, member))

        # Determine the type of the foreign key
        column = create_table_column(cls, cls._id)
        return (column.type, sa.ForeignKey(name, **kwargs))
    elif issubclass(cls, str):
        return sa.String(**kwargs)
    elif issubclass(cls, int):
        return sa.Integer(**kwargs)
    elif issubclass(cls, float):
        return sa.Float(**kwargs)
    elif issubclass(cls, dict):
        return sa.JSON(**kwargs)
    elif issubclass(cls, (tuple, list)):
        return sa.ARRAY(**kwargs)
    elif issubclass(cls, datetime.datetime):
        return sa.DateTime(**kwargs)
    elif issubclass(cls, datetime.date):
        return sa.Date(**kwargs)
    elif issubclass(cls, datetime.time):
        return sa.Time(**kwargs)
    elif issubclass(cls, datetime.timedelta):
        return sa.Interval(**kwargs)
    elif issubclass(cls, (bytes, bytearray)):
        return sa.LargeBinary(**kwargs)
    elif issubclass(cls, Decimal):
        return sa.Numeric(**kwargs)
    raise NotImplementedError(
        f"A type for {member.name} of {model} ({cls}) could not be "
        f"determined automatically, please specify it manually by tagging it "
        f"with .tag(column=<sqlalchemy column>) or set `store=False`"
    )


def resolve_member_types(member: Member) -> Optional[TupleType[type, ...]]:
    """Determine the type specified on a member to determine ForeignKey
    relations.

    Parameters
    ----------
    member: atom.catom.Member
        The member to retrieve the type from
    Returns
    -------
    types: Optional[Tuple[Model or object, ..]]
        The member types.

    """
    if hasattr(member, "resolve"):
        types = member.resolve()  # type: ignore
    else:
        types = member.validate_mode[-1]
    if types is None or isinstance(types, tuple):
        return types
    return (types,)


def resolve_member_column(
    model: Type["SQLModel"], field: str, related_clauses: Optional[ListType[str]] = None
) -> sa.Column:
    """Get the sqlalchemy column for the given model and field.

    Parameters
    ----------
    model: atomdb.sql.Model
        The model to lookup
    field: String
        The field name

    Returns
    -------
    result: sa.Column
        A tuple containing the through table (or None) and the
        sqlalchemy column.

    """
    if model is None or not field:
        raise ValueError("Invalid field %s on %s" % (field, model))

    # Walk the relations
    if "__" in field:
        path = field
        *related_parts, field = field.split("__")
        clause = "__".join(related_parts)
        if related_clauses is not None and clause not in related_clauses:
            related_clauses.append(clause)

        # Follow the FK lookups
        # Rename so the original lookup path is retained if an error occurs
        rel_model = model
        for part in related_parts:
            m = rel_model.members().get(part)
            if m is None:
                raise ValueError("Invalid field %s on %s" % (path, model))
            rel_model_types = resolve_member_types(m)
            if rel_model_types is None:
                raise ValueError("Invalid field %s on %s" % (path, model))
            rel_model = rel_model_types[0]
        model = rel_model

    # Lookup the member
    m = model.members().get(field)
    if m is not None:
        if m.metadata:
            # If the field has a different name assigned use that
            field = m.metadata.get("name", field)
        if isinstance(m, Relation):
            # Support looking up columns through a relation by the pk
            model = m.to  # type: ignore

            # Add the through table to the related clauses if needed
            if related_clauses is not None and field not in related_clauses:
                related_clauses.append(field)

            field = model.__pk__

    # Finally get the column from the table
    col = model.objects.table.columns.get(field)
    if col is None:
        raise ValueError("Invalid field %s on %s" % (field, model))
    return col


def atom_member_to_sql_column(
    model: Type["SQLModel"], member: Member, **kwargs
) -> TypeEngine:
    """Convert the atom member type to an sqlalchemy table column type
    See https://docs.sqlalchemy.org/en/latest/core/type_basics.html

    """
    if hasattr(member, "get_column_type"):
        # Allow custom members to define the column type programatically
        return member.get_column_type(model)  # type: ignore
    elif isinstance(member, api.Str):
        return sa.String(**kwargs)
    elif hasattr(api, "Unicode") and isinstance(member, api.Unicode):  # type: ignore
        return sa.Unicode(**kwargs)  # type: ignore
    elif isinstance(member, api.Bool):
        return sa.Boolean()
    elif isinstance(member, api.Int):
        return sa.Integer()
    elif hasattr(api, "Long") and isinstance(member, api.Long):  # type: ignore
        return sa.BigInteger()
    elif isinstance(member, api.Float):
        return sa.Float()
    elif isinstance(member, api.Range):
        # TODO: Add min / max
        return sa.Integer()
    elif isinstance(member, api.FloatRange):
        # TODO: Add min / max
        return sa.Float()
    elif isinstance(member, api.Enum):
        return sa.Enum(*member.items, name=member.name)
    elif hasattr(api, "IntEnum") and isinstance(member, api.IntEnum):  # type: ignore
        return sa.SmallInteger()
    elif isinstance(member, FK_TYPES):
        value_type = resolve_member_types(member)
        if value_type is None:
            raise TypeError("Instance and Typed members must specify types")
        return py_type_to_sql_column(model, member, value_type, **kwargs)
    elif isinstance(member, Relation):
        # Relations are for backrefs
        item_type = member.validate_mode[-1]
        if item_type is None:
            raise TypeError("Relation members must specify types")

        # Resolve the item type
        value_type = resolve_member_types(item_type)
        if value_type is None:
            raise TypeError("Relation members must specify types")
        return None  # Relations are just syntactic sugar
    elif isinstance(member, (api.List, api.ContainerList, api.Tuple)):
        item_type = member.validate_mode[-1]
        if item_type is None:
            raise TypeError("List and Tuple members must specify types")

        # Resolve the item type
        value_type = resolve_member_types(item_type)
        if value_type is None:
            raise TypeError("List and Tuple members must specify types")
        if issubclass(value_type[0], JSONModel):
            return sa.JSON(**kwargs)
        t = py_type_to_sql_column(model, member, value_type, **kwargs)
        if isinstance(t, tuple):
            t = t[0]  # Use only the value type
        return sa.ARRAY(t)
    elif isinstance(member, api.Bytes):
        return sa.LargeBinary(**kwargs)
    elif isinstance(member, api.Dict):
        return sa.JSON(**kwargs)
    raise NotImplementedError(
        f"A column for {member.name} of {model} could not be determined "
        f"automatically, please specify it manually by tagging it "
        f"with .tag(column=<sqlalchemy column>)"
    )


def create_table_column(model: Type["SQLModel"], member: Member) -> sa.Column:
    """Converts an Atom member into a sqlalchemy data type.

    Parameters
    ----------
    model: Model
        The model which owns this member
    member: Member
        The atom member

    Returns
    -------
    column: Column
        An sqlalchemy column

    References
    ----------
    1. https://docs.sqlalchemy.org/en/latest/core/types.html

    """
    get_column = getattr(member, "get_column", None)
    if get_column is not None:
        # Allow custom members to define the column programatically
        return get_column(model)

    # Copy the metadata as we modify it
    metadata = member.metadata.copy() if member.metadata else {}

    # If a column is specified use that
    if "column" in metadata:
        return metadata["column"]

    metadata.pop("store", None)
    column_name = metadata.pop("name", member.name)
    column_type = metadata.pop("type", None)

    # Extract column kwargs from member metadata
    kwargs = {}
    for k in COLUMN_KWARGS:
        if k in metadata:
            kwargs[k] = metadata.pop(k)

    if column_type is None:
        args = atom_member_to_sql_column(model, member, **metadata)
        if args is None:
            return None
        if not isinstance(args, (tuple, list)):
            args = (args,)
    elif isinstance(column_type, (tuple, list)):
        args = column_type
    else:
        args = (column_type,)
    return sa.Column(column_name, *args, **kwargs)


def create_table(model: Type["SQLModel"], metadata: sa.MetaData) -> sa.Table:
    """Create an sqlalchemy table by inspecting the Model and generating
    a column for each member.

    Parameters
    ----------
    model: SQLModel
        The atom model

    References
    ----------
    1. https://docs.sqlalchemy.org/en/latest/core/metadata.html

    """
    name = model.__model__
    members = model.members()
    args = []

    # Add columns
    for f in model.__fields__:
        column = create_table_column(model, members[f])
        if column is not None:
            args.append(column)

    # Add table metadata
    meta = getattr(model, "Meta", None)
    if meta:
        # Abstract field
        abstract = getattr(meta, "abstract", False)
        if abstract:
            raise NotImplementedError(
                f"Tables cannot be created for abstract models: {model}"
            )

        # Unique constraints
        unique_together = getattr(meta, "unique_together", None)
        if unique_together is not None:
            if not isinstance(unique_together, (tuple, list)):
                raise TypeError("Meta unique_together must be a tuple or list")
            if isinstance(unique_together[0], str):
                unique_together = [unique_together]
            for constraint in unique_together:
                if isinstance(constraint, (tuple, list)):
                    constraint = sa.UniqueConstraint(*constraint)
                args.append(constraint)

        # Check constraints
        constraints = getattr(meta, "constraints", None)
        if constraints is not None:
            if not isinstance(constraints, (tuple, list)):
                raise TypeError("Meta constraints must be a tuple or list")
            args.extend(constraints)

        # Composite indexes
        composite_indexes = getattr(meta, "composite_indexes", None)
        if composite_indexes is not None:
            if not isinstance(composite_indexes, (tuple, list)):
                raise TypeError("Meta composite_indexes must be a tuple or list")
            for index in composite_indexes:
                if not isinstance(index, (tuple, list)):
                    raise TypeError("Index must be a tuple or list")
                args.extend([schema.Index(*index)])

    # Create table
    table = sa.Table(name, metadata, *args)

    # Hook up any database triggers defined
    triggers = getattr(meta, "triggers", None)
    if triggers is not None:
        if isinstance(triggers, dict):
            triggers = list(triggers.items())
        elif not isinstance(triggers, (tuple, list)):
            raise TypeError("Meta triggers must be a dict, tuple, or list")
        for event, trigger in triggers:
            # Allow triggers to be a lambda that generates one
            if not isinstance(trigger, sa.schema.DDL) and callable(trigger):
                trigger = trigger()
            sa.event.listen(table, event, trigger)

    return table


class SQLModelSerializer(ModelSerializer):
    """Uses sqlalchemy to lookup the model."""

    def flatten_object(self, obj: Model, scope: ScopeType) -> Any:
        """Serialize a model for entering into the database

        Parameters
        ----------
        obj: Model
            The object to unflatten
        scope: Dict
            The scope of references available for circular lookups

        Returns
        -------
        result: Object
            The flattened object

        """
        return obj._id

    async def get_object_state(self, obj, state, scope):
        """Load the object state if needed. Since the __model__ is not saved
        to the db tables with SQL we know that if it's "probably" there
        because a query was used.
        """
        ModelType = obj.__class__
        if "__model__" in state:
            return state  # Joined already
        q = ModelType.objects.query(None, _id=state["_id"])
        return await ModelType.objects.fetchone(q)

    def _default_registry(self):
        """Add all sql and json models to the registry"""
        registry = JSONSerializer.instance().registry.copy()
        registry.update({m.__model__: m for m in find_sql_models()})
        return registry


class SQLModelManager(ModelManager):
    """Manages models via aiopg, aiomysql, or similar libraries supporting
    SQLAlchemy tables. It stores a table for each class and when accessed
    on a Model subclass it returns a table proxy binding.

    """

    #: Constraint naming convenctions
    conventions = Dict(default=CONSTRAINT_NAMING_CONVENTIONS)

    #: Metadata
    metadata = Instance(sa.MetaData)

    #: Table proxy cache
    proxies = Dict()

    #: Cache results
    cache = Bool(True)

    def _default_metadata(self) -> sa.MetaData:
        binding = SQLBinding(manager=self)
        return sa.MetaData(binding, naming_convention=self.conventions)

    def create_tables(self) -> DictType[Type["SQLModel"], sa.Table]:
        """Create sqlalchemy tables for all registered SQLModels"""
        tables = {}
        for cls in find_sql_models():
            table = cls.__table__
            if table is None:
                table = cls.__table__ = create_table(cls, self.metadata)
            if not table.metadata.bind:
                table.metadata.bind = SQLBinding(manager=self, table=table)
            tables[cls] = table
        return tables

    def __get__(
        self, obj: T, cls: Optional[Type[T]] = None
    ) -> Union["SQLTableProxy[T]", "SQLModelManager"]:
        """Retrieve the table for the requested object or class."""
        cls = cls or obj.__class__
        if not issubclass(cls, Model):
            return self  # Only return the client when used from a Model
        proxy = self.proxies.get(cls)
        if proxy is None:
            table = cls.__table__
            if table is None:
                table = cls.__table__ = create_table(cls, self.metadata)
            proxy = self.proxies[cls] = SQLTableProxy(table=table, model=cls)
        return proxy

    def _default_database(self):
        raise EnvironmentError(
            "No database engine has been set. Use "
            "SQLModelManager.instance().database = <db>"
        )


class ConnectionProxy(Atom):
    """An wapper for a connection to be used with async with syntax that
    does nothing but passes the existing connection when entered.

    """

    connection = Value()

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        pass


class SQLTableProxy(Atom, Generic[T]):
    #: Table this is a proxy to
    table = Instance(sa.Table, optional=False)

    #: Model which owns the table
    model = ForwardSubclass(lambda: SQLModel)

    #: Cache of pk: obj using weakrefs
    cache = Typed(weakref.WeakValueDictionary, ())

    #: Key used to pull the connection out of filter kwargs
    connection_kwarg = Str("connection")

    #: Reference to the aiomysql or aiopg Engine
    #: This is used to get a connection from the connection pool.
    @property
    def engine(self):
        """Retrieve the database engine."""
        db = self.table.bind.manager.database
        if isinstance(db, dict):
            return db[self.model.__database__]
        return db

    def connection(self, connection=None):
        """Create a new connection or the return given connection as an async
        contextual object.

        Parameters
        ----------
        connection: Database connection or None
            The connection to return

        Returns
        -------
        connection: Database connection
            The database connection or one that may be used with async with

        """
        if connection is None:
            return self.engine.acquire()
        return ConnectionProxy(connection=connection)

    def create_table(self):
        """A wrapper for create which catches the create queries then executes
        them
        """
        table = self.table
        table.create()
        return table.bind.wait()

    def drop_table(self):
        table = self.table
        table.drop()
        return table.bind.wait()

    async def execute(self, *args, **kwargs):
        connection = kwargs.pop(self.connection_kwarg, None)
        async with self.connection(connection) as conn:
            return await conn.execute(*args, **kwargs)

    async def fetchall(self, query: QueryType, connection=None):
        """Fetch all results for the query.

        Parameters
        ----------
        query: String or Query
            The query to execute
        connection: Database connection
            The connection to use or a new one will be created

        Returns
        -------
        rows; List
            List of rows returned, NOT objects

        """
        async with self.connection(connection) as conn:
            r = await conn.execute(query)
            return await r.fetchall()

    async def fetchmany(self, query, size=None, connection=None):
        """Fetch size results for the query.

        Parameters
        ----------
        query: String or Query
            The query to execute
        size: Int or None
            The number of results to fetch
        connection: Database connection
            The connection to use or a new one will be created

        Returns
        -------
        rows: List
            List of rows returned, NOT objects

        """
        async with self.connection(connection) as conn:
            r = await conn.execute(query)
            return await r.fetchmany(size)

    async def fetchone(self, query: QueryType, connection=None):
        """Fetch a single result for the query.

        Parameters
        ----------
        query: String or Query
            The query to execute
        connection: Database connection
            The connection to use or a new one will be created

        Returns
        -------
        rows: Object or None
            The row returned or None
        """
        async with self.connection(connection) as conn:
            r = await conn.execute(query)
            return await r.fetchone()

    async def scalar(self, query: QueryType, connection=None):
        """Fetch the scalar result for the query.

        Parameters
        ----------
        query: String or Query
            The query to execute
        connection: Database connection
            The connection to use or a new one will be created

        Returns
        -------
        result: Object or None
            The the first column of the first row or None
        """
        async with self.connection(connection) as conn:
            r = await conn.execute(query)
            return await r.scalar()

    async def get_or_create(self, **filters) -> TupleType[T, bool]:
        """Get or create a model matching the given criteria

        Parameters
        ----------
        filters: Dict
            The filters to use to retrieve the object

        Returns
        -------
        result: Tuple[Model, Bool]
            A tuple of the object and a bool indicating if it was just created

        """
        obj = await self.get(**filters)
        if obj is not None:
            return (obj, False)
        connection_kwarg = self.connection_kwarg
        connection = filters.get(connection_kwarg)
        state = {
            k: v for k, v in filters.items() if "__" not in k and k != connection_kwarg
        }
        obj = self.model(**state)
        await obj.save(force_insert=True, connection=connection)
        return (obj, True)

    async def create(self, **state) -> T:
        """Create a and save model with the given state.

        The connection parameter is popped from this state.

        Parameters
        ----------
        state: Dict
            The state to use to initialize the object.

        Returns
        -------
        result: Tuple[Model, Bool]
            A tuple of the object and a bool indicating if it was just created

        """
        connection = state.pop(self.connection_kwarg, None)
        obj = cast(T, self.model(**state))
        await obj.save(force_insert=True, connection=connection)
        return obj

    def __getattr__(self, name: str):
        """All other fields are delegated to the query set"""
        qs: SQLQuerySet[T] = SQLQuerySet(proxy=self)
        return getattr(qs, name)


class SQLQuerySet(Atom, Generic[T]):
    #: Proxy
    proxy = Instance(SQLTableProxy, optional=False)
    connection = Value()

    filter_clauses = List()
    related_clauses = List()
    outer_join = Bool()
    order_clauses = List()
    distinct_clauses = List()
    limit_count = Int()
    query_offset = Int()

    def clone(self, **kwargs) -> "SQLQuerySet[T]":
        state = self.__getstate__()
        state.update(kwargs)
        return self.__class__(**state)

    def query(self, query_type: str = "select", *columns, **kwargs):
        if kwargs:
            return self.filter(**kwargs).query(query_type)
        p = self.proxy
        from_table = p.table
        tables = [from_table]
        model = p.model
        members = model.members()
        use_labels = bool(self.related_clauses)
        outer_join = self.outer_join
        for clause in self.related_clauses:
            from_table = p.table
            for part in clause.split("__"):
                m = members.get(part)
                assert m is not None
                rel_model_types = resolve_member_types(m)
                assert rel_model_types is not None
                rel_model = rel_model_types[0]
                assert issubclass(rel_model, Model)
                table = rel_model.objects.table
                from_table = sa.join(from_table, table, isouter=outer_join)
                tables.append(table)

        if query_type == "select":
            q = sa.select(columns or tables, use_labels=use_labels)
            q = q.select_from(from_table)
        elif query_type == "delete":
            q = sa.delete(from_table)
        elif query_type == "update":
            q = sa.update(from_table)
        else:
            raise ValueError("Unsupported query type")

        if self.distinct_clauses:
            q = q.distinct(*self.distinct_clauses)

        if self.filter_clauses:
            if len(self.filter_clauses) == 1:
                q = q.where(self.filter_clauses[0])
            else:
                q = q.where(sa.and_(*self.filter_clauses))

        if self.order_clauses:
            q = q.order_by(*self.order_clauses)

        if self.limit_count:
            q = q.limit(self.limit_count)

        if self.query_offset:
            q = q.offset(self.query_offset)

        return q

    def select_related(
        self, *related: Sequence[str], outer_join: Optional[bool] = None
    ) -> "SQLQuerySet[T]":
        """Define related fields to join in the query.

        Parameters
        ----------
        args: List[str]
            List of related fields to join.
        outer_join: Bool
            If given set whether or not a left outer join is used.

        Returns
        -------
        query: SQLQuerySet
            A clone of this queryset with the related field terms added.

        """
        outer_join = self.outer_join if outer_join is None else outer_join
        related_clauses = self.related_clauses + list(related)
        return self.clone(related_clauses=related_clauses, outer_join=outer_join)

    def order_by(self, *args):
        """Order the query by the given fields.

        Parameters
        ----------
        args: List[str or column]
            Fields to order by. A "-" prefix denotes decending.

        Returns
        -------
        query: SQLQuerySet
            A clone of this queryset with the ordering terms added.

        """
        order_clauses = self.order_clauses[:]
        related_clauses = self.related_clauses[:]
        model = self.proxy.model
        for arg in args:
            if isinstance(arg, str):
                # Convert django-style to sqlalchemy ordering column
                if arg[0] == "-":
                    field = arg[1:]
                    ascending = False
                else:
                    field = arg
                    ascending = True

                col = resolve_member_column(model, field, related_clauses)

                if ascending:
                    clause = col.asc()
                else:
                    clause = col.desc()
            else:
                clause = arg
            if clause not in order_clauses:
                order_clauses.append(clause)
        return self.clone(order_clauses=order_clauses, related_clauses=related_clauses)

    def distinct(self, *args):
        """Apply distinct on the given column.

        Parameters
        ----------
        args: List[str or column]
            Fields that must be distinct.

        Returns
        -------
        query: SQLQuerySet
            A clone of this queryset with the distinct terms added.

        """
        distinct_clauses = self.distinct_clauses[:]
        related_clauses = self.related_clauses[:]
        model = self.proxy.model
        for arg in args:
            if isinstance(arg, str):
                # Convert name to sqlalchemy column
                clause = resolve_member_column(model, arg, related_clauses)
            else:
                clause = arg
            if clause not in distinct_clauses:
                distinct_clauses.append(clause)
        return self.clone(
            distinct_clauses=distinct_clauses, related_clauses=related_clauses
        )

    def filter(self, *args, **kwargs: DictType[str, Any]):
        """Filter the query by the given parameters. This accepts sqlalchemy
        filters by arguments and django-style parameters as kwargs.

        Parameters
        ----------
        args: List
            List of sqlalchemy filters
        kwargs: Dict[str, object]
            Django style filters to use

        Returns
        -------
        query: SQLQuerySet
            A clone of this queryset with the filter terms added.

        """
        p = self.proxy
        filter_clauses = self.filter_clauses + list(args)
        related_clauses = self.related_clauses[:]

        connection_kwarg = p.connection_kwarg
        connection = self.connection

        # Build the filter operations
        for k, v in kwargs.items():
            # Ignore connection parameter
            if k == connection_kwarg:
                connection = v
                continue
            model = p.model
            op = "eq"
            if "__" in k:
                parts = k.split("__")
                if parts[-1] in QUERY_OPS:
                    op = parts[-1]
                    k = "__".join(parts[:-1])
            col = resolve_member_column(model, k, related_clauses)

            # Support lookups by model
            if isinstance(v, Model):
                v = v.serializer.flatten_object(v, scope={})
            elif op in ("in", "notin"):
                # Flatten lists when using in or notin ops
                v = model.serializer.flatten(v, scope={})

            clause = getattr(col, QUERY_OPS[op])(v)
            filter_clauses.append(clause)

        return self.clone(
            connection=connection,
            filter_clauses=filter_clauses,
            related_clauses=related_clauses,
        )

    def __getitem__(self, key):
        if isinstance(key, slice):
            offset = key.start or 0
            limit = key.stop - key.start if key.stop else 0
        elif isinstance(key, int):
            limit = 1
            offset = key
        else:
            raise TypeError("Invalid key")
        if offset < 0:
            raise ValueError("Cannot use a negative offset")
        if limit < 0:
            raise ValueError("Cannot use a negative limit")
        return self.clone(limit_count=limit, query_offset=offset)

    def limit(self, limit: int):
        return self.clone(limit_count=limit)

    def offset(self, offset: int):
        return self.clone(query_offset=offset)

    # -------------------------------------------------------------------------
    # Query execution API
    # -------------------------------------------------------------------------
    async def values(
        self,
        *args,
        distinct: bool = False,
        flat: bool = False,
        group_by: Optional[Sequence[Union[str, sa.Column]]] = None,
    ) -> Sequence[Any]:
        """Returns the results as a list of dict instead of models.

        Parameters
        ----------
        args: List[str or column]
            List of columns to select
        distinct: Bool
            Return only distinct rows
        flat: Bool
            Requires exactly one arg and will flatten the result into a single
            list of values.
        group_by: List[str or column]
            Optional Columns to group by

        Returns
        -------
        results: List
            List of results depending on the parameters described above

        """
        if flat and len(args) != 1:
            raise ValueError("Values with flat=True can only have one param")
        if args:
            model = self.proxy.model
            columns = []
            for col in args:
                if isinstance(col, str):
                    col = resolve_member_column(model, col)
                columns.append(col)
            q = self.query("select", *columns)
        else:
            q = self.query("select")
        if group_by is not None:
            q = q.group_by(group_by)
        if distinct:
            q = q.distinct()
        cursor = await self.proxy.fetchall(q, connection=self.connection)
        if flat:
            return [row[0] for row in cursor]
        return cursor

    async def count(self, *args, **kwargs) -> int:
        if args or kwargs:
            return await self.filter(*args, **kwargs).count()
        subq = self.query("select").alias("subquery")
        q = sa.func.count().select().select_from(subq)
        return await self.proxy.scalar(q, connection=self.connection)

    def max(self, *columns):
        return self.aggregate(*columns, func=sa.func.max)

    def min(self, *columns):
        return self.aggregate(*columns, func=sa.func.min)

    def mode(self, *columns):
        return self.aggregate(*columns, func=sa.func.mode)

    def sum(self, *columns):
        return self.aggregate(*columns, func=sa.func.sum)

    def aggregate(self, *args, func=None):
        model = self.proxy.model
        columns = []
        for col in args:
            if isinstance(col, str):
                col = resolve_member_column(model, col)
            columns.append(func(col) if func is not None else col)
        subq = self.query("select").alias("subquery")
        q = sa.select(columns).select_from(subq)
        return self.proxy.fetchone(q, connection=self.connection)

    async def exists(self, *args, **kwargs) -> bool:
        if args or kwargs:
            return await self.filter(*args, **kwargs).exists()
        q = sa.exists(self.query("select")).select()
        return await self.proxy.scalar(q, connection=self.connection)

    async def delete(self, *args, **kwargs):
        if args or kwargs:
            return await self.filter(*args, **kwargs).delete()
        q = self.query("delete")
        return await self.proxy.execute(q, connection=self.connection)

    async def update(self, **values):
        """Perform an update of the given values."""
        # Translate any renamed fields back to the database value
        for py_name, db_name in self.proxy.model.__renamed_fields__.items():
            if py_name in values:
                values[db_name] = values.pop(py_name)
        q = self.query("update").values(**values)
        return await self.proxy.execute(q, connection=self.connection)

    def __await__(self):
        # So await Model.objects.filter() works
        f = asyncio.ensure_future(self.all())
        yield from f
        return f.result()

    async def all(self, *args, **kwargs) -> Sequence[T]:
        if args or kwargs:
            return await self.filter(*args, **kwargs).all()
        q = self.query("select")
        restore = self.proxy.model.restore
        cursor = await self.proxy.fetchall(q, connection=self.connection)
        return [cast(T, await restore(row)) for row in cursor]

    async def get(self, *args, **kwargs) -> Optional[T]:
        """Get the first result matching the query. Unlike django this will
        NOT raise an error if multiple objects would be returned or an entry
        does not exist.

        Returns
        -------
        model: Optional[Model]
            The first entry matching the query

        """
        if args or kwargs:
            return await self.filter(*args, **kwargs).get()
        q = self.query("select")
        row = await self.proxy.fetchone(q, connection=self.connection)
        if row is None:
            return None
        return cast(T, await self.proxy.model.restore(row))


class SQLBinding(Atom):
    #: Model Manager
    manager = Instance(SQLModelManager)

    #: The queue
    queue = ContainerList()

    engine = property(lambda s: s)

    @property
    def name(self):
        return self.dialect.name

    @property
    def dialect(self):
        """Get the dialect of the database."""
        db = self.manager.database
        if isinstance(db, dict):
            db = db["default"]
        return db.dialect

    def schema_for_object(self, obj):
        return obj.schema

    def contextual_connect(self, **kwargs):
        return self

    def connect(self, **kwargs):
        return self

    def execution_options(self, **kw):
        return self

    def compiler(self, statement, parameters, **kwargs):
        return self.dialect.compiler(statement, parameters, engine=self, **kwargs)

    def create(self, entity, **kwargs):
        kwargs["checkfirst"] = False
        node = ddl.SchemaGenerator(self.dialect, self, **kwargs)
        node.traverse_single(entity)

    def drop(self, entity, **kwargs):
        kwargs["checkfirst"] = False
        node = ddl.SchemaDropper(self.dialect, self, **kwargs)
        node.traverse_single(entity)

    def _run_ddl_visitor(self, visitorcallable, element, connection=None, **kwargs):
        kwargs["checkfirst"] = False
        visitorcallable(self.dialect, self, **kwargs).traverse_single(element)

    def _run_visitor(self, visitorcallable, element, connection=None, **kwargs):
        kwargs["checkfirst"] = False
        node = visitorcallable(self.dialect, self, **kwargs)
        node.traverse_single(element)

    def execute(self, object_, *multiparams, **params):
        self.queue.append((object_, multiparams, params))

    async def wait(self):
        db = self.manager.database
        if isinstance(db, dict):
            engine = db["default"]
        else:
            engine = db
        result = None
        async with engine.acquire() as conn:
            try:
                while self.queue:
                    op, args, kwargs = self.queue.pop(0)
                    result = await conn.execute(op, args)
            finally:
                self.queue = []  # Wipe queue on error
        return result


class SQLMeta(ModelMeta):
    """Both the pk and _id are aliases to the primary key"""

    def __new__(meta, name, bases, dct):
        cls = ModelMeta.__new__(meta, name, bases, dct)

        members = cls.members()

        # If a member tagged with primary_key=True is defined,
        # on this class, use that as the primary key and reassign
        # the _id member to alias the new primary key.
        pk: Member = cls._id
        for name, m in members.items():
            if name == "_id":
                continue
            if m.metadata and m.metadata.get("primary_key"):
                if pk.name != "_id" and m.name != pk.name:
                    raise NotImplementedError(
                        "Using multiple primary keys is not yet supported. "
                        f"Both {pk.name} and {m.name} are marked as primary."
                    )
                pk = m

        if pk is not cls._id:
            # Workaround member index generation issue
            # TODO: Remove this
            old_index = cls._id.index
            if old_index > 0 and pk.index != old_index:
                pk.set_index(old_index)

            # Reassign the _id field to the primary key member.
            cls._id = members["_id"] = pk

            # Remove "_id" from the fields list as it is now an alias
            cls.__fields__ = tuple((f for f in cls.__fields__ if f != "_id"))

        # Check that the atom member indexes are still valid after
        # reassinging to avoid a bug in the past.
        member_indices = set()
        for name, m in members.items():
            if name == "_id":
                continue  # The _id is an alias
            assert m.index not in member_indices
            member_indices.add(m.index)

        # Set the pk name
        cls.__pk__ = (pk.metadata or {}).get("name", pk.name)

        # Set to the sqlalchemy Table
        cls.__table__ = None

        # Will be set to the table model by manager, not done here to avoid
        # import errors that may occur
        cls.__backrefs__ = set()

        # If a Meta class is defined check it's validity and if extending
        # do not inherit the abstract attribute
        Meta = dct.get("Meta", None)
        if Meta is not None:
            for f in dir(Meta):
                if f.startswith("_"):
                    continue
                if f not in VALID_META_FIELDS:
                    raise TypeError(f"{f} is not a valid Meta field on {cls}.")

            db_table = getattr(Meta, "db_table", None)
            if db_table:
                cls.__model__ = db_table

            db_name = getattr(Meta, "db_name", None)
            if db_name:
                cls.__database__ = db_name

        # If this inherited from an abstract model but didn't specify
        # Meta info make the subclass not abstract unless it was redefined
        base_meta = getattr(cls, "Meta", None)
        if base_meta and getattr(base_meta, "abstract", None):
            if not Meta:

                class Meta(base_meta):
                    abstract = False

                cls.Meta = Meta
            elif getattr(Meta, "abstract", None) is None:
                Meta.abstract = False

        # Create a set of fields to remove from state before saving to the db
        # this removes Relation instances and several needed for json
        excluded_fields = cls.__excluded_fields__ = {"__model__", "__ref__", "_id"}

        for name, member in cls.members().items():
            if isinstance(member, Relation):
                excluded_fields.add(name)

        # Cache the mapping of any renamed fields
        renamed_fields = cls.__renamed_fields__ = {}
        for old_name, member in cls.members().items():
            if old_name in excluded_fields:
                continue  # Ignore excluded fields
            if member.metadata:
                new_name = member.metadata.get("name")
                if new_name is not None:
                    renamed_fields[old_name] = new_name

        return cls


class SQLModel(Model, metaclass=SQLMeta):
    """A model that can be saved and restored to and from a database supported
    by sqlalchemy.

    """

    #: Primary key field name
    __pk__: ClassVar[str]

    #: Models which link back to this
    __backrefs__: ClassVar[SetType[TupleType[Type["Model"], Member]]]

    #: List of fields which have been tagged with a different column name
    #: Mapping is class attr -> database column name.
    __renamed_fields__: ClassVar[DictType[str, str]]

    #: Set of fields to exclude from the database
    __excluded_fields__: ClassVar[SetType[str]]

    #: Reference to the sqlalchemy table backing this model
    __table__: ClassVar[Optional[sa.Table]]

    #: Database name. If the `database` field of the manager is a dict
    #: This field will be used to determine which engine to use.
    __database__: ClassVar[str] = "default"

    #: If no other member is tagged with primary_key=True this is used
    _id = Typed(int).tag(store=True, primary_key=True)

    #: Use SQL serializer
    serializer = SQLModelSerializer.instance()

    #: Use SQL object manager
    objects = SQLModelManager.instance()

    @classmethod
    async def restore(
        cls: Type[T], state: StateType, force: Optional[bool] = None, **kwargs: Any
    ) -> T:
        """Restore an object from the database using the primary key. Save
        a ref in the table's object cache.  If force is True, update
        the cache if it exists.
        """
        try:
            # When sqlalchemy does a join the key will have a prefix
            # of the database name
            pk = state[f"{cls.__model__}_{cls.__pk__}"]
        except KeyError:
            pk = state[cls.__pk__]

        # Check the default for force reloading
        if force is None:
            force = not cls.objects.table.bind.manager.cache

        # Check if this is in the cache
        cache = cls.objects.cache
        obj = cache.get(pk)
        if obj is None:
            # Create and cache it
            obj = cls.__new__(cls)
            cache[pk] = obj

            # This ideally should only be done if created
            await obj.__restorestate__(state)
        elif force or not obj.__restored__:
            await obj.__restorestate__(state)

        return obj

    async def __restorestate__(
        self: T, state: StateType, scope: Optional[ScopeType] = None
    ):
        # Holds cleaned state extracted for this model which may come from
        # a DB row using labels or renamed columns
        cleaned_state: StateType = {}

        # Check if the state is using labels by looking for the pk field
        pk_label = f"{self.__model__}_{self.__pk__}"

        if pk_label in state:
            # Convert row to dict because it speeds up lookups
            state = dict(state)
            # Convert the joined tables into nested states
            table = self.objects.table
            table_name = table.name
            pk = state[pk_label]

            # Pull known
            for name, m in self.members().items():
                metadata = m.metadata or {}
                field_name = metadata.get("name", name)
                field_label = f"{table_name}_{field_name}"

                if isinstance(m, FK_TYPES):
                    RelModelTypes = resolve_member_types(m)
                    assert RelModelTypes is not None
                    RelModel = RelModelTypes[0]
                    if issubclass(RelModel, SQLModel):
                        # If the related model was joined, the pk field should
                        # exist so automatically restore that as well
                        rel_pk_name = f"{RelModel.__model__}_{RelModel.__pk__}"
                        try:
                            rel_id = state[field_label]
                        except KeyError:
                            rel_id = state.get(rel_pk_name)
                        if rel_id:
                            # Lookup in cache first to avoid recursion errors
                            cache = RelModel.objects.cache
                            obj = cache.get(rel_id)
                            if obj is None:
                                if rel_pk_name in state:
                                    obj = await RelModel.restore(state)
                                else:
                                    # Create an unloaded model
                                    obj = RelModel.__new__(RelModel)
                                    cache[rel_id] = obj
                                    obj._id = rel_id
                            cleaned_state[name] = obj
                            continue

                elif isinstance(m, Relation):
                    # Through must be a callable which returns a tuple of
                    # the through table model
                    through_factory = metadata.get("through")
                    if through_factory:
                        M2M, this_attr, rel_attr = through_factory()
                        cleaned_state[name] = [
                            getattr(r, rel_attr)
                            for r in await M2M.objects.filter(**{this_attr: pk})
                        ]
                    else:
                        # Skip relations
                        continue

                # Regular fields
                try:
                    cleaned_state[name] = state[field_label]
                except KeyError:
                    continue

        else:
            # If any column names were redefined use those instead
            for name, m in self.members().items():
                field_name = (m.metadata or {}).get("name", name)

                try:
                    v = state[field_name]
                except KeyError:
                    continue

                # Attempt to lookup related fields from the cache
                if v is not None and isinstance(m, FK_TYPES):
                    RelModelTypes = resolve_member_types(m)
                    assert RelModelTypes is not None
                    RelModel = RelModelTypes[0]
                    if issubclass(RelModel, SQLModel):
                        cache = RelModel.objects.cache
                        obj = cache.get(v)
                        if obj is None:
                            # Create an unloaded model
                            obj = RelModel.__new__(RelModel)
                            cache[v] = obj
                            obj._id = v
                        v = obj
                    elif issubclass(RelModel, JSONModel):
                        v = await RelModel.restore(v)

                cleaned_state[name] = v
        await super().__restorestate__(cleaned_state, scope)

    async def load(
        self: T,
        connection=None,
        reload: bool = False,
        fields: Optional[Sequence[str]] = None,
    ):
        """Alias to load this object from the database

        Parameters
        ----------
        connection: Connection
            The connection instance to use in a transaction
        reload: Bool
            If True force reloading the state even if the state has
            already been loaded.
        fields: Sequence[str]
            Optional list of field names to load. Use this to refresh
            specific fields from the database.

        """
        skip = self.__restored__ and not reload and not fields
        if skip or self._id is None:
            return  # Already loaded or won't do anything
        db = self.objects
        t = db.table
        if fields is not None:
            renamed = self.__renamed_fields__
            columns = (t.c[renamed.get(f, f)] for f in fields)
            q = sa.select(columns).select_from(t)
        else:
            q = t.select()
        q = q.where(t.c[self.__pk__] == self._id)
        state = await db.fetchone(q, connection=connection)
        await self.__restorestate__(state)

    async def save(
        self: T,
        force_insert: bool = False,
        force_update: bool = False,
        update_fields: Optional[Sequence[str]] = None,
        connection=None,
    ):
        """Alias to save this object to the database

        Parameters
        ----------
        force_insert: Bool
            Ensure that save performs an insert
        force_update: Bool
            Ensure that save performs an update
        update_fields: Iterable[str]
            If given, only update the given fields
        connection: Connection
            The connection instance to use in a transaction

        Returns
        -------
        result: Value
            Update or save result

        """
        if force_insert and force_update:
            raise ValueError("Cannot use force_insert and force_update together")

        db = self.objects
        state = self.__getstate__()

        # Remove any fields are in the state but should not go into the db
        for f in self.__excluded_fields__:
            state.pop(f, None)

        # Replace any renamed fields
        for py_name, db_name in self.__renamed_fields__.items():
            state[db_name] = state.pop(py_name)

        table = db.table
        async with db.connection(connection) as conn:
            if force_update or (self._id and not force_insert):

                # If update fields was given, only pass those
                if update_fields is not None:
                    # Replace any update fields with the appropriate name
                    renamed = self.__renamed_fields__
                    update_fields = [renamed.get(f, f) for f in update_fields]

                    # Replace update fields with only those given
                    state = {f: state[f] for f in update_fields}

                q = (
                    table.update()
                    .where(table.c[self.__pk__] == self._id)
                    .values(**state)
                )
                r = await conn.execute(q)
                if not r.rowcount:
                    log.warning(
                        f'Did not update "{self}", either no rows with '
                        f"pk={self._id} exist or it has not changed."
                    )
            else:
                if not self._id:
                    # Postgres errors if using None for the pk
                    state.pop(self.__pk__, None)
                q = table.insert().values(**state)
                r = await conn.execute(q)

                # Don't overwrite if force inserting
                if not self._id:
                    if hasattr(r, "lastrowid"):
                        self._id = r.lastrowid  # MySQL
                    else:
                        self._id = await r.scalar()  # Postgres

                # Save a ref to the object in the model cache
                db.cache[self._id] = self
            self.__restored__ = True
            return r

    async def delete(self: T, connection=None):
        """Alias to delete this object in the database"""
        pk = self._id
        if not pk:
            return
        db = self.objects
        table = db.table  # type: sa.Table
        q = table.delete().where(table.c[self.__pk__] == pk)
        async with db.connection(connection) as conn:
            r = await conn.execute(q)
            if not r.rowcount:
                log.warning(
                    f'Did not delete "{self}", no rows with ' f"pk={self._id} exist."
                )
            del db.cache[pk]
            del self._id
            return r
