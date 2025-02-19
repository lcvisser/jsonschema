import datetime
import ipaddress
import re

from jsonschema.exceptions import FormatError


class FormatChecker(object):
    """
    A ``format`` property checker.

    JSON Schema does not mandate that the ``format`` property actually do any
    validation. If validation is desired however, instances of this class can
    be hooked into validators to enable format validation.

    `FormatChecker` objects always return ``True`` when asked about
    formats that they do not know how to validate.

    To check a custom format using a function that takes an instance and
    returns a ``bool``, use the `FormatChecker.checks` or
    `FormatChecker.cls_checks` decorators.

    Arguments:

        formats (~collections.abc.Iterable):

            The known formats to validate. This argument can be used to
            limit which formats will be used during validation.
    """

    checkers = {}

    def __init__(self, formats=None):
        if formats is None:
            self.checkers = self.checkers.copy()
        else:
            self.checkers = dict((k, self.checkers[k]) for k in formats)

    def __repr__(self):
        return "<FormatChecker checkers={}>".format(sorted(self.checkers))

    def checks(self, format, raises=()):
        """
        Register a decorated function as validating a new format.

        Arguments:

            format (str):

                The format that the decorated function will check.

            raises (Exception):

                The exception(s) raised by the decorated function when an
                invalid instance is found.

                The exception object will be accessible as the
                `jsonschema.exceptions.ValidationError.cause` attribute of the
                resulting validation error.
        """

        def _checks(func):
            self.checkers[format] = (func, raises)
            return func
        return _checks

    cls_checks = classmethod(checks)

    def check(self, instance, format):
        """
        Check whether the instance conforms to the given format.

        Arguments:

            instance (*any primitive type*, i.e. str, number, bool):

                The instance to check

            format (str):

                The format that instance should conform to


        Raises:

            FormatError: if the instance does not conform to ``format``
        """

        if format not in self.checkers:
            return

        func, raises = self.checkers[format]
        result, cause = None, None
        try:
            result = func(instance)
        except raises as e:
            cause = e
        if not result:
            raise FormatError(
                "%r is not a %r" % (instance, format), cause=cause,
            )

    def conforms(self, instance, format):
        """
        Check whether the instance conforms to the given format.

        Arguments:

            instance (*any primitive type*, i.e. str, number, bool):

                The instance to check

            format (str):

                The format that instance should conform to

        Returns:

            bool: whether it conformed
        """

        try:
            self.check(instance, format)
        except FormatError:
            return False
        else:
            return True


draft3_format_checker = FormatChecker()
draft4_format_checker = FormatChecker()
draft6_format_checker = FormatChecker()
draft7_format_checker = FormatChecker()


_draft_checkers = dict(
    draft3=draft3_format_checker,
    draft4=draft4_format_checker,
    draft6=draft6_format_checker,
    draft7=draft7_format_checker,
)


def _checks_drafts(
    name=None,
    draft3=None,
    draft4=None,
    draft6=None,
    draft7=None,
    raises=(),
):
    draft3 = draft3 or name
    draft4 = draft4 or name
    draft6 = draft6 or name
    draft7 = draft7 or name

    def wrap(func):
        if draft3:
            func = _draft_checkers["draft3"].checks(draft3, raises)(func)
        if draft4:
            func = _draft_checkers["draft4"].checks(draft4, raises)(func)
        if draft6:
            func = _draft_checkers["draft6"].checks(draft6, raises)(func)
        if draft7:
            func = _draft_checkers["draft7"].checks(draft7, raises)(func)

        # Oy. This is bad global state, but relied upon for now, until
        # deprecation. See https://github.com/Julian/jsonschema/issues/519
        # and test_format_checkers_come_with_defaults
        FormatChecker.cls_checks(draft7 or draft6 or draft4 or draft3, raises)(
            func,
        )
        return func
    return wrap


@_checks_drafts(name="idn-email")
@_checks_drafts(name="email")
def is_email(instance):
    if not isinstance(instance, str):
        return True
    return "@" in instance


@_checks_drafts(
    draft3="ip-address",
    draft4="ipv4",
    draft6="ipv4",
    draft7="ipv4",
    raises=ipaddress.AddressValueError,
)
def is_ipv4(instance):
    if not isinstance(instance, str):
        return True
    return ipaddress.IPv4Address(instance)


@_checks_drafts(name="ipv6", raises=ipaddress.AddressValueError)
def is_ipv6(instance):
    if not isinstance(instance, str):
        return True
    address = ipaddress.IPv6Address(instance)
    return not getattr(address, "scope_id", "")


try:
    from fqdn import FQDN
except ImportError:
    pass
else:
    @_checks_drafts(
        draft3="host-name",
        draft4="hostname",
        draft6="hostname",
        draft7="hostname",
    )
    def is_host_name(instance):
        if not isinstance(instance, str):
            return True
        return FQDN(instance).is_valid


try:
    # The built-in `idna` codec only implements RFC 3890, so we go elsewhere.
    import idna
except ImportError:
    pass
else:
    @_checks_drafts(
        draft7="idn-hostname",
        raises=(idna.IDNAError, UnicodeError),
    )
    def is_idn_host_name(instance):
        if not isinstance(instance, str):
            return True
        idna.encode(instance)
        return True


try:
    import rfc3987
except ImportError:
    try:
        from rfc3986_validator import validate_rfc3986
    except ImportError:
        pass
    else:
        @_checks_drafts(name="uri")
        def is_uri(instance):
            if not isinstance(instance, str):
                return True
            return validate_rfc3986(instance, rule="URI")

        @_checks_drafts(
            draft6="uri-reference",
            draft7="uri-reference",
            raises=ValueError,
        )
        def is_uri_reference(instance):
            if not isinstance(instance, str):
                return True
            return validate_rfc3986(instance, rule="URI_reference")

else:
    @_checks_drafts(draft7="iri", raises=ValueError)
    def is_iri(instance):
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="IRI")

    @_checks_drafts(draft7="iri-reference", raises=ValueError)
    def is_iri_reference(instance):
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="IRI_reference")

    @_checks_drafts(name="uri", raises=ValueError)
    def is_uri(instance):
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="URI")

    @_checks_drafts(
        draft6="uri-reference",
        draft7="uri-reference",
        raises=ValueError,
    )
    def is_uri_reference(instance):
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="URI_reference")


try:
    from strict_rfc3339 import validate_rfc3339
except ImportError:
    try:
        from rfc3339_validator import validate_rfc3339
    except ImportError:
        validate_rfc3339 = None

if validate_rfc3339:
    @_checks_drafts(name="date-time")
    def is_datetime(instance):
        if not isinstance(instance, str):
            return True
        return validate_rfc3339(instance.upper())

    @_checks_drafts(draft7="time")
    def is_time(instance):
        if not isinstance(instance, str):
            return True
        return is_datetime("1970-01-01T" + instance)


@_checks_drafts(name="regex", raises=re.error)
def is_regex(instance):
    if not isinstance(instance, str):
        return True
    return re.compile(instance)


if hasattr(datetime.date, "fromisoformat"):
    _is_date = datetime.date.fromisoformat
else:
    def _is_date(instance):
        return datetime.datetime.strptime(instance, "%Y-%m-%d")


@_checks_drafts(draft3="date", draft7="date", raises=ValueError)
def is_date(instance):
    if not isinstance(instance, str):
        return True
    return _is_date(instance)


@_checks_drafts(draft3="time", raises=ValueError)
def is_draft3_time(instance):
    if not isinstance(instance, str):
        return True
    return datetime.datetime.strptime(instance, "%H:%M:%S")


try:  # webcolors>=1.11
    from webcolors import CSS21_NAMES_TO_HEX
    import webcolors
except ImportError:
    try:  # webcolors<1.11
        from webcolors import css21_names_to_hex as CSS21_NAMES_TO_HEX
        import webcolors
    except ImportError:
        pass
else:
    def is_css_color_code(instance):
        return webcolors.normalize_hex(instance)

    @_checks_drafts(draft3="color", raises=(ValueError, TypeError))
    def is_css21_color(instance):
        if (
            not isinstance(instance, str) or
            instance.lower() in CSS21_NAMES_TO_HEX
        ):
            return True
        return is_css_color_code(instance)

    def is_css3_color(instance):
        if instance.lower() in webcolors.css3_names_to_hex:
            return True
        return is_css_color_code(instance)


try:
    import jsonpointer
except ImportError:
    pass
else:
    @_checks_drafts(
        draft6="json-pointer",
        draft7="json-pointer",
        raises=jsonpointer.JsonPointerException,
    )
    def is_json_pointer(instance):
        if not isinstance(instance, str):
            return True
        return jsonpointer.JsonPointer(instance)

    # TODO: I don't want to maintain this, so it
    #       needs to go either into jsonpointer (pending
    #       https://github.com/stefankoegl/python-json-pointer/issues/34) or
    #       into a new external library.
    @_checks_drafts(
        draft7="relative-json-pointer",
        raises=jsonpointer.JsonPointerException,
    )
    def is_relative_json_pointer(instance):
        # Definition taken from:
        # https://tools.ietf.org/html/draft-handrews-relative-json-pointer-01#section-3
        if not isinstance(instance, str):
            return True
        non_negative_integer, rest = [], ""
        for i, character in enumerate(instance):
            if character.isdigit():
                non_negative_integer.append(character)
                continue

            if not non_negative_integer:
                return False

            rest = instance[i:]
            break
        return (rest == "#") or jsonpointer.JsonPointer(rest)


try:
    import uri_template
except ImportError:
    pass
else:
    @_checks_drafts(
        draft6="uri-template",
        draft7="uri-template",
    )
    def is_uri_template(instance):
        if not isinstance(instance, str):
            return True
        return uri_template.validate(instance)
