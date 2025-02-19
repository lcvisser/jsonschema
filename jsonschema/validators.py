"""
Creation and extension of validators, with implementations for existing drafts.
"""
from collections.abc import Sequence
from functools import lru_cache
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.request import urlopen
from warnings import warn
import contextlib
import json

from jsonschema import (
    _legacy_validators,
    _types,
    _utils,
    _validators,
    exceptions,
)
# Sigh. https://gitlab.com/pycqa/flake8/issues/280
#       https://github.com/pyga/ebb-lint/issues/7
# Imported for backwards compatibility.
from jsonschema.exceptions import ErrorTree

ErrorTree


validators = {}
meta_schemas = _utils.URIDict()


def validates(version):
    """
    Register the decorated validator for a ``version`` of the specification.

    Registered validators and their meta schemas will be considered when
    parsing ``$schema`` properties' URIs.

    Arguments:

        version (str):

            An identifier to use as the version's name

    Returns:

        collections.abc.Callable:

            a class decorator to decorate the validator with the version
    """

    def _validates(cls):
        validators[version] = cls
        meta_schema_id = cls.ID_OF(cls.META_SCHEMA)
        if meta_schema_id:
            meta_schemas[meta_schema_id] = cls
        return cls
    return _validates


def _id_of(schema):
    if schema is True or schema is False:
        return u""
    return schema.get(u"$id", u"")


def create(
    meta_schema,
    validators=(),
    version=None,
    type_checker=_types.draft7_type_checker,
    id_of=_id_of,
):
    """
    Create a new validator class.

    Arguments:

        meta_schema (collections.abc.Mapping):

            the meta schema for the new validator class

        validators (collections.abc.Mapping):

            a mapping from names to callables, where each callable will
            validate the schema property with the given name.

            Each callable should take 4 arguments:

                1. a validator instance,
                2. the value of the property being validated within the
                   instance
                3. the instance
                4. the schema

        version (str):

            an identifier for the version that this validator class will
            validate. If provided, the returned validator class will
            have its ``__name__`` set to include the version, and also
            will have `jsonschema.validators.validates` automatically
            called for the given version.

        type_checker (jsonschema.TypeChecker):

            a type checker, used when applying the :validator:`type` validator.

            If unprovided, a `jsonschema.TypeChecker` will be created
            with a set of default types typical of JSON Schema drafts.

        id_of (collections.abc.Callable):

            A function that given a schema, returns its ID.

    Returns:

        a new `jsonschema.IValidator` class
    """

    class Validator:

        VALIDATORS = dict(validators)
        META_SCHEMA = dict(meta_schema)
        TYPE_CHECKER = type_checker
        ID_OF = staticmethod(id_of)

        def __init__(
            self,
            schema,
            resolver=None,
            format_checker=None,
        ):
            if resolver is None:
                resolver = RefResolver.from_schema(schema, id_of=id_of)

            self.resolver = resolver
            self.format_checker = format_checker
            self.schema = schema

        @classmethod
        def check_schema(cls, schema):
            for error in cls(cls.META_SCHEMA).iter_errors(schema):
                raise exceptions.SchemaError.create_from(error)

        def iter_errors(self, instance, _schema=None):
            if _schema is None:
                _schema = self.schema

            if _schema is True:
                return
            elif _schema is False:
                yield exceptions.ValidationError(
                    "False schema does not allow %r" % (instance,),
                    validator=None,
                    validator_value=None,
                    instance=instance,
                    schema=_schema,
                )
                return

            scope = id_of(_schema)
            if scope:
                self.resolver.push_scope(scope)
            try:
                ref = _schema.get(u"$ref")
                if ref is not None:
                    validators = [(u"$ref", ref)]
                else:
                    validators = _schema.items()

                for k, v in validators:
                    validator = self.VALIDATORS.get(k)
                    if validator is None:
                        continue

                    errors = validator(self, v, instance, _schema) or ()
                    for error in errors:
                        # set details if not already set by the called fn
                        error._set(
                            validator=k,
                            validator_value=v,
                            instance=instance,
                            schema=_schema,
                        )
                        if k not in {u"if", u"$ref"}:
                            error.schema_path.appendleft(k)
                        yield error
            finally:
                if scope:
                    self.resolver.pop_scope()

        def descend(self, instance, schema, path=None, schema_path=None):
            for error in self.iter_errors(instance, schema):
                if path is not None:
                    error.path.appendleft(path)
                if schema_path is not None:
                    error.schema_path.appendleft(schema_path)
                yield error

        def validate(self, *args, **kwargs):
            for error in self.iter_errors(*args, **kwargs):
                raise error

        def is_type(self, instance, type):
            try:
                return self.TYPE_CHECKER.is_type(instance, type)
            except exceptions.UndefinedTypeCheck:
                raise exceptions.UnknownType(type, instance, self.schema)

        def is_valid(self, instance, _schema=None):
            error = next(self.iter_errors(instance, _schema), None)
            return error is None

    if version is not None:
        Validator = validates(version)(Validator)
        Validator.__name__ = version.title().replace(" ", "") + "Validator"

    return Validator


def extend(validator, validators=(), version=None, type_checker=None):
    """
    Create a new validator class by extending an existing one.

    Arguments:

        validator (jsonschema.IValidator):

            an existing validator class

        validators (collections.abc.Mapping):

            a mapping of new validator callables to extend with, whose
            structure is as in `create`.

            .. note::

                Any validator callables with the same name as an
                existing one will (silently) replace the old validator
                callable entirely, effectively overriding any validation
                done in the "parent" validator class.

                If you wish to instead extend the behavior of a parent's
                validator callable, delegate and call it directly in
                the new validator function by retrieving it using
                ``OldValidator.VALIDATORS["validator_name"]``.

        version (str):

            a version for the new validator class

        type_checker (jsonschema.TypeChecker):

            a type checker, used when applying the :validator:`type` validator.

            If unprovided, the type checker of the extended
            `jsonschema.IValidator` will be carried along.

    Returns:

        a new `jsonschema.IValidator` class extending the one provided

    .. note:: Meta Schemas

        The new validator class will have its parent's meta schema.

        If you wish to change or extend the meta schema in the new
        validator class, modify ``META_SCHEMA`` directly on the returned
        class. Note that no implicit copying is done, so a copy should
        likely be made before modifying it, in order to not affect the
        old validator.
    """

    all_validators = dict(validator.VALIDATORS)
    all_validators.update(validators)

    if type_checker is None:
        type_checker = validator.TYPE_CHECKER
    return create(
        meta_schema=validator.META_SCHEMA,
        validators=all_validators,
        version=version,
        type_checker=type_checker,
        id_of=validator.ID_OF,
    )


Draft3Validator = create(
    meta_schema=_utils.load_schema("draft3"),
    validators={
        u"$ref": _validators.ref,
        u"additionalItems": _validators.additionalItems,
        u"additionalProperties": _validators.additionalProperties,
        u"dependencies": _legacy_validators.dependencies_draft3,
        u"disallow": _legacy_validators.disallow_draft3,
        u"divisibleBy": _validators.multipleOf,
        u"enum": _validators.enum,
        u"extends": _legacy_validators.extends_draft3,
        u"format": _validators.format,
        u"items": _legacy_validators.items_draft3_draft4,
        u"maxItems": _validators.maxItems,
        u"maxLength": _validators.maxLength,
        u"maximum": _legacy_validators.maximum_draft3_draft4,
        u"minItems": _validators.minItems,
        u"minLength": _validators.minLength,
        u"minimum": _legacy_validators.minimum_draft3_draft4,
        u"pattern": _validators.pattern,
        u"patternProperties": _validators.patternProperties,
        u"properties": _legacy_validators.properties_draft3,
        u"type": _legacy_validators.type_draft3,
        u"uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft3_type_checker,
    version="draft3",
    id_of=lambda schema: schema.get(u"id", ""),
)

Draft4Validator = create(
    meta_schema=_utils.load_schema("draft4"),
    validators={
        u"$ref": _validators.ref,
        u"additionalItems": _validators.additionalItems,
        u"additionalProperties": _validators.additionalProperties,
        u"allOf": _validators.allOf,
        u"anyOf": _validators.anyOf,
        u"dependencies": _validators.dependencies,
        u"enum": _validators.enum,
        u"format": _validators.format,
        u"items": _legacy_validators.items_draft3_draft4,
        u"maxItems": _validators.maxItems,
        u"maxLength": _validators.maxLength,
        u"maxProperties": _validators.maxProperties,
        u"maximum": _legacy_validators.maximum_draft3_draft4,
        u"minItems": _validators.minItems,
        u"minLength": _validators.minLength,
        u"minProperties": _validators.minProperties,
        u"minimum": _legacy_validators.minimum_draft3_draft4,
        u"multipleOf": _validators.multipleOf,
        u"not": _validators.not_,
        u"oneOf": _validators.oneOf,
        u"pattern": _validators.pattern,
        u"patternProperties": _validators.patternProperties,
        u"properties": _validators.properties,
        u"required": _validators.required,
        u"type": _validators.type,
        u"uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft4_type_checker,
    version="draft4",
    id_of=lambda schema: schema.get(u"id", ""),
)

Draft6Validator = create(
    meta_schema=_utils.load_schema("draft6"),
    validators={
        u"$ref": _validators.ref,
        u"additionalItems": _validators.additionalItems,
        u"additionalProperties": _validators.additionalProperties,
        u"allOf": _validators.allOf,
        u"anyOf": _validators.anyOf,
        u"const": _validators.const,
        u"contains": _validators.contains,
        u"dependencies": _validators.dependencies,
        u"enum": _validators.enum,
        u"exclusiveMaximum": _validators.exclusiveMaximum,
        u"exclusiveMinimum": _validators.exclusiveMinimum,
        u"format": _validators.format,
        u"items": _validators.items,
        u"maxItems": _validators.maxItems,
        u"maxLength": _validators.maxLength,
        u"maxProperties": _validators.maxProperties,
        u"maximum": _validators.maximum,
        u"minItems": _validators.minItems,
        u"minLength": _validators.minLength,
        u"minProperties": _validators.minProperties,
        u"minimum": _validators.minimum,
        u"multipleOf": _validators.multipleOf,
        u"not": _validators.not_,
        u"oneOf": _validators.oneOf,
        u"pattern": _validators.pattern,
        u"patternProperties": _validators.patternProperties,
        u"properties": _validators.properties,
        u"propertyNames": _validators.propertyNames,
        u"required": _validators.required,
        u"type": _validators.type,
        u"uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft6_type_checker,
    version="draft6",
)

Draft7Validator = create(
    meta_schema=_utils.load_schema("draft7"),
    validators={
        u"$ref": _validators.ref,
        u"additionalItems": _validators.additionalItems,
        u"additionalProperties": _validators.additionalProperties,
        u"allOf": _validators.allOf,
        u"anyOf": _validators.anyOf,
        u"const": _validators.const,
        u"contains": _validators.contains,
        u"dependencies": _validators.dependencies,
        u"enum": _validators.enum,
        u"exclusiveMaximum": _validators.exclusiveMaximum,
        u"exclusiveMinimum": _validators.exclusiveMinimum,
        u"format": _validators.format,
        u"if": _validators.if_,
        u"items": _validators.items,
        u"maxItems": _validators.maxItems,
        u"maxLength": _validators.maxLength,
        u"maxProperties": _validators.maxProperties,
        u"maximum": _validators.maximum,
        u"minItems": _validators.minItems,
        u"minLength": _validators.minLength,
        u"minProperties": _validators.minProperties,
        u"minimum": _validators.minimum,
        u"multipleOf": _validators.multipleOf,
        u"oneOf": _validators.oneOf,
        u"not": _validators.not_,
        u"pattern": _validators.pattern,
        u"patternProperties": _validators.patternProperties,
        u"properties": _validators.properties,
        u"propertyNames": _validators.propertyNames,
        u"required": _validators.required,
        u"type": _validators.type,
        u"uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft7_type_checker,
    version="draft7",
)

_LATEST_VERSION = Draft7Validator


class RefResolver(object):
    """
    Resolve JSON References.

    Arguments:

        base_uri (str):

            The URI of the referring document

        referrer:

            The actual referring document

        store (dict):

            A mapping from URIs to documents to cache

        cache_remote (bool):

            Whether remote refs should be cached after first resolution

        handlers (dict):

            A mapping from URI schemes to functions that should be used
            to retrieve them

        urljoin_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of joining
            the resolution scope to subscopes.

        remote_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of
            resolved remote URLs.

    Attributes:

        cache_remote (bool):

            Whether remote refs should be cached after first resolution
    """

    def __init__(
        self,
        base_uri,
        referrer,
        store=(),
        cache_remote=True,
        handlers=(),
        urljoin_cache=None,
        remote_cache=None,
    ):
        if urljoin_cache is None:
            urljoin_cache = lru_cache(1024)(urljoin)
        if remote_cache is None:
            remote_cache = lru_cache(1024)(self.resolve_from_url)

        self.referrer = referrer
        self.cache_remote = cache_remote
        self.handlers = dict(handlers)

        self._scopes_stack = [base_uri]
        self.store = _utils.URIDict(
            (id, validator.META_SCHEMA)
            for id, validator in meta_schemas.items()
        )
        self.store.update(store)
        self.store[base_uri] = referrer

        self._urljoin_cache = urljoin_cache
        self._remote_cache = remote_cache

    @classmethod
    def from_schema(cls, schema, id_of=_id_of, *args, **kwargs):
        """
        Construct a resolver from a JSON schema object.

        Arguments:

            schema:

                the referring schema

        Returns:

            `RefResolver`
        """

        return cls(base_uri=id_of(schema), referrer=schema, *args, **kwargs)

    def push_scope(self, scope):
        """
        Enter a given sub-scope.

        Treats further dereferences as being performed underneath the
        given scope.
        """
        self._scopes_stack.append(
            self._urljoin_cache(self.resolution_scope, scope),
        )

    def pop_scope(self):
        """
        Exit the most recent entered scope.

        Treats further dereferences as being performed underneath the
        original scope.

        Don't call this method more times than `push_scope` has been
        called.
        """
        try:
            self._scopes_stack.pop()
        except IndexError:
            raise exceptions.RefResolutionError(
                "Failed to pop the scope from an empty stack. "
                "`pop_scope()` should only be called once for every "
                "`push_scope()`"
            )

    @property
    def resolution_scope(self):
        """
        Retrieve the current resolution scope.
        """
        return self._scopes_stack[-1]

    @property
    def base_uri(self):
        """
        Retrieve the current base URI, not including any fragment.
        """
        uri, _ = urldefrag(self.resolution_scope)
        return uri

    @contextlib.contextmanager
    def in_scope(self, scope):
        """
        Temporarily enter the given scope for the duration of the context.
        """
        self.push_scope(scope)
        try:
            yield
        finally:
            self.pop_scope()

    @contextlib.contextmanager
    def resolving(self, ref):
        """
        Resolve the given ``ref`` and enter its resolution scope.

        Exits the scope on exit of this context manager.

        Arguments:

            ref (str):

                The reference to resolve
        """

        url, resolved = self.resolve(ref)
        self.push_scope(url)
        try:
            yield resolved
        finally:
            self.pop_scope()

    def resolve(self, ref):
        """
        Resolve the given reference.
        """
        url = self._urljoin_cache(self.resolution_scope, ref)
        return url, self._remote_cache(url)

    def resolve_from_url(self, url):
        """
        Resolve the given remote URL.
        """
        url, fragment = urldefrag(url)
        try:
            document = self.store[url]
        except KeyError:
            try:
                document = self.resolve_remote(url)
            except Exception as exc:
                raise exceptions.RefResolutionError(exc)

        return self.resolve_fragment(document, fragment)

    def resolve_fragment(self, document, fragment):
        """
        Resolve a ``fragment`` within the referenced ``document``.

        Arguments:

            document:

                The referent document

            fragment (str):

                a URI fragment to resolve within it
        """

        fragment = fragment.lstrip(u"/")
        parts = unquote(fragment).split(u"/") if fragment else []

        for part in parts:
            part = part.replace(u"~1", u"/").replace(u"~0", u"~")

            if isinstance(document, Sequence):
                # Array indexes should be turned into integers
                try:
                    part = int(part)
                except ValueError:
                    pass
            try:
                document = document[part]
            except (TypeError, LookupError):
                raise exceptions.RefResolutionError(
                    "Unresolvable JSON pointer: %r" % fragment
                )

        return document

    def resolve_remote(self, uri):
        """
        Resolve a remote ``uri``.

        If called directly, does not check the store first, but after
        retrieving the document at the specified URI it will be saved in
        the store if :attr:`cache_remote` is True.

        .. note::

            If the requests_ library is present, ``jsonschema`` will use it to
            request the remote ``uri``, so that the correct encoding is
            detected and used.

            If it isn't, or if the scheme of the ``uri`` is not ``http`` or
            ``https``, UTF-8 is assumed.

        Arguments:

            uri (str):

                The URI to resolve

        Returns:

            The retrieved document

        .. _requests: https://pypi.org/project/requests/
        """
        try:
            import requests
        except ImportError:
            requests = None

        scheme = urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif scheme in [u"http", u"https"] and requests:
            # Requests has support for detecting the correct encoding of
            # json over http
            result = requests.get(uri).json()
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                result = json.loads(url.read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


def validate(instance, schema, cls=None, *args, **kwargs):
    """
    Validate an instance under the given schema.

        >>> validate([2, 3, 4], {"maxItems": 2})
        Traceback (most recent call last):
            ...
        ValidationError: [2, 3, 4] is too long

    :func:`validate` will first verify that the provided schema is
    itself valid, since not doing so can lead to less obvious error
    messages and fail in less obvious or consistent ways.

    If you know you have a valid schema already, especially if you
    intend to validate multiple instances with the same schema, you
    likely would prefer using the `IValidator.validate` method directly
    on a specific validator (e.g. ``Draft7Validator.validate``).


    Arguments:

        instance:

            The instance to validate

        schema:

            The schema to validate with

        cls (IValidator):

            The class that will be used to validate the instance.

    If the ``cls`` argument is not provided, two things will happen
    in accordance with the specification. First, if the schema has a
    :validator:`$schema` property containing a known meta-schema [#]_
    then the proper validator will be used. The specification recommends
    that all schemas contain :validator:`$schema` properties for this
    reason. If no :validator:`$schema` property is found, the default
    validator class is the latest released draft.

    Any other provided positional and keyword arguments will be passed
    on when instantiating the ``cls``.

    Raises:

        `jsonschema.exceptions.ValidationError` if the instance
            is invalid

        `jsonschema.exceptions.SchemaError` if the schema itself
            is invalid

    .. rubric:: Footnotes
    .. [#] known by a validator registered with
        `jsonschema.validators.validates`
    """
    if cls is None:
        cls = validator_for(schema)

    cls.check_schema(schema)
    validator = cls(schema, *args, **kwargs)
    error = exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def validator_for(schema, default=_LATEST_VERSION):
    """
    Retrieve the validator class appropriate for validating the given schema.

    Uses the :validator:`$schema` property that should be present in the
    given schema to look up the appropriate validator class.

    Arguments:

        schema (collections.abc.Mapping or bool):

            the schema to look at

        default:

            the default to return if the appropriate validator class
            cannot be determined.

            If unprovided, the default is to return the latest supported
            draft.
    """
    if schema is True or schema is False or u"$schema" not in schema:
        return default
    if schema[u"$schema"] not in meta_schemas:
        warn(
            (
                "The metaschema specified by $schema was not found. "
                "Using the latest draft to validate, but this will raise "
                "an error in the future."
            ),
            DeprecationWarning,
            stacklevel=2,
        )
    return meta_schemas.get(schema[u"$schema"], _LATEST_VERSION)


def set_validator_for(schema, validator, meta_schema_id=None):
    """
    Register a validator class for the given schema.

    Uses the `$schema` property of the given schema as key in the validator
    lookup table, unless `meta_schema_id` is given to override this.

    This method allows you to set a validator for a particular schema in cases
    where automatic lookup fails, for example when a schema specificies a
    `$schema` that is not resolvable.

    Arguments:

        schema (collections.abc.Mapping):

            the schema to register a validator for

        validator:

            the validator to use for this schema

        meta_schema_id:

            if not `None`, the given value is used as key in the lookup table.
    """
    if meta_schema_id is None:
        meta_schema_id = schema.get(u"$schema", u"")

    if meta_schema_id:
        meta_schemas[meta_schema_id] = validator
