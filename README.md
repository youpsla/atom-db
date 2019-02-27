[![Build Status](https://travis-ci.org/codelv/atom-db.svg?branch=master)](https://travis-ci.org/codelv/atom-db)
[![codecov](https://codecov.io/gh/codelv/atom-db/branch/master/graph/badge.svg)](https://codecov.io/gh/codelv/atom-db)

atom-db is a database abstraction layer for the
[atom](https://github.com/nucleic/atom) framework. This package provides api's for
seamlessly saving and restoring atom objects from json based document databases
and (coming soon) SQL databases supported by sqlalchemy.


### Why?

The main reason for building this is to make it easier have database integration
with [enaml](https://github.com/nucleic/enaml) applications.  Without this,
a separate framework is needed to define database models, which is a
duplication of work.

This was originally a part of [enaml-web](https://github.com/codelv/enaml-web)
but has been pulled out to a separate package.


### Structure

The design is based somewhat on django. Using `Model.objects` retrieves a
manager for that type of object which can be used to create queries. No
restriction is imposed on what type of manager is used, leaving that to
whichever database library is preferred (ex motor, txmongo, sqlalchemy,...).

In addition to `Model.objects` a serializer is added to each class as
`Model.serializer` which is used to serialize and deserialize the objects
to and from the database.


### Example using MongoDB and motor

Just define models using atom members, but subclass the NoSQLModel.

```python

from atom.api import Unicode, Int, Instance, List
from atomdb.nosql import NoSQLModel, NoSQLModelManager
from motor.motor_asyncio import AsyncIOMotorClient

# Set DB
client = AsyncIOMotorClient()
mgr = NoSQLModelManager.instance()
mgr.database = client.test_db


class Group(NoSQLModel):
    name = Unicode()

class User(NoSQLModel):
    name = Unicode()
    age = Int()
    groups = List(Group)


```

Then we can create an instance and save it. It will perform an upsert or replace
the existing entry.

```python

admins = Group(name="Admins")
await admins.save()

# It will save admins using it's ObjectID
bob = User(name="Bob", age=32, groups=[admins])
await bob.save()

tom = User(name="Tom", age=34, groups=[admins])
await tom.save()

```

To fetch from the DB each model has a `ModelManager` called `objects` that will
simply return the collection for the model type. For example.

```python

# Fetch from db, you can use any MongoDB queries here
state = await User.objects.find_one({'name': "James"})
if state:
    james = await User.restore(state)

# etc...
```

Restoring is async because it will automatically fetch any related objects
(ex the groups in this case). It saves objects using the ObjectID when present.

And finally you can either delete using queries on the manager directly or
call on the object.

```python
await tom.delete()
assert not await User.objects.find_one({'name': "Tom"})

```

You can exclude members from being saved to the DB by tagging them
with `.tag(store=False)`.


### SQL with aiomysql / aiopg

> SQL support is currently a WIP. Currently only basic operations like
creation of tables, dropping tables, and simple queries are working
(no joins work yet).

Just define models using atom members, but subclass the SQLModel.

Tag members with information needed for sqlalchemy tables, ex
`Str().tag(length=40)` will make a `sa.String(40)`.
See https://docs.sqlalchemy.org/en/latest/core/type_basics.html



### Contributing

This is early in development and may have issues. Pull requests,
feature requests, are welcome!
