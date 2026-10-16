"""Cairn's own errors raised from the caller's call.

The public functions (`cairn.use`, `cairn.pull`, `cairn.publish`, ...) and the public methods and properties of the
object API (`Repo`, `Artifact`, `Version`, `Draft`, `Run`, ...) go through `public` (or the class decorator
`surface`). A StoreError (NotFound, Conflict, Unreachable, Forbidden, ...: an error Cairn raises on purpose, with a
message that says what was looked for and where) leaves them with its traceback cut at that call, so Python prints
the caller's frames and one line for the call they made, then the message:

    Traceback (most recent call last):
      File "train.py", line 3, in <module>
        data = cairn.use("datasets/quadratic@latest")
      File ".../cairn/api.py", line 49, in cairn.use
        raise error.with_traceback(None) from None  # a Cairn error: its message says what and where
    cairn.store.errors.NotFound: no artifact datasets/quadratic in the repo /path/.cairn (found from the working directory)

Any other exception (a bug) keeps its full traceback, and so does every error with `CAIRN_DEBUG=1`. Nothing is
swallowed and no exception hook is installed.
"""

from __future__ import annotations

import functools
import os

from .store.errors import StoreError

DEBUG = "CAIRN_DEBUG"


def public(name: str):
    """A decorator for a public function or method, shown as `name` (e.g. `cairn.use`, `Version.files`) in a
    traceback: a StoreError it raises ends there, with no inner frames and no chained exceptions."""

    def decorate(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except StoreError as error:
                if os.environ.get(DEBUG):
                    raise
                error.__cause__ = error.__context__ = None
                raise error.with_traceback(None) from None  # a Cairn error: its message says what and where

        call.__code__ = call.__code__.replace(co_name=name, co_qualname=name)
        call.__wrapped_public__ = True
        return call

    return decorate


SPECIAL = ("__init__", "__enter__", "__exit__", "__iter__", "__getattr__", "__setattr__")


def surface(cls):
    """A class decorator: every public method and property of `cls` (and its constructor and context-manager methods)
    goes through `public`, named `<Class>.<attribute>`."""
    for key, value in list(vars(cls).items()):
        if key.startswith("_") and key not in SPECIAL:
            continue
        label = cls.__name__ if key == "__init__" else f"{cls.__name__}.{key}"
        if isinstance(value, property):
            setattr(cls, key, property(_wrap(label, value.fget), _wrap(label, value.fset), value.fdel, value.__doc__))
        elif isinstance(value, (staticmethod, classmethod)):
            setattr(cls, key, type(value)(_wrap(label, value.__func__)))
        elif callable(value) and not isinstance(value, type):
            setattr(cls, key, _wrap(label, value))
    return cls


def _wrap(label: str, function):
    if function is None or getattr(function, "__wrapped_public__", False):
        return function
    return public(label)(function)
