import time
import typing
import typing as _t
from datetime import datetime
from datetime import timezone

from .encoding import base64_decode
from .encoding import base64_encode
from .encoding import bytes_to_int
from .encoding import int_to_bytes
from .encoding import want_bytes
from .exc import BadSignature
from .exc import BadTimeSignature
from .exc import SignatureExpired
from .serializer import Serializer
from .signer import Signer

_t_str_bytes = _t.Union[str, bytes]
_t_opt_str_bytes = _t.Optional[_t_str_bytes]
_t_opt_int = _t.Optional[int]

if _t.TYPE_CHECKING:
    import typing_extensions as _te


class TimestampSigner(Signer):
    """Works like the regular :class:`.Signer` but also records the time
    of the signing and can be used to expire signatures. The
    :meth:`unsign` method can raise :exc:`.SignatureExpired` if the
    unsigning failed because the signature is expired.
    """

    def get_timestamp(self) -> int:
        """Returns the current timestamp. The function must return an
        integer.
        """
        return int(time.time())

    def timestamp_to_datetime(self, ts: int) -> datetime:
        """Convert the timestamp from :meth:`get_timestamp` into an
        aware :class`datetime.datetime` in UTC.

        .. versionchanged:: 2.0
            The timestamp is returned as a timezone-aware ``datetime``
            in UTC rather than a naive ``datetime`` assumed to be UTC.
        """
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def sign(self, value: _t_str_bytes) -> bytes:
        """Signs the given string and also attaches time information."""
        value = want_bytes(value)
        timestamp = base64_encode(int_to_bytes(self.get_timestamp()))
        sep = want_bytes(self.sep)
        value = value + sep + timestamp
        return value + sep + self.get_signature(value)

    # Ignore overlapping signatures check, return_timestamp is the only
    # parameter that affects the return type.

    @typing.overload
    def unsign(  # type: ignore
        self,
        signed_value: _t_str_bytes,
        max_age: _t_opt_int = None,
        return_timestamp: "_te.Literal[False]" = False,
    ) -> bytes:
        ...

    @typing.overload
    def unsign(
        self,
        signed_value: _t_str_bytes,
        max_age: _t_opt_int = None,
        return_timestamp: "_te.Literal[True]" = True,
    ) -> _t.Tuple[bytes, datetime]:
        ...

    def unsign(
        self,
        signed_value: _t_str_bytes,
        max_age: _t_opt_int = None,
        return_timestamp: bool = False,
    ) -> _t.Union[_t.Tuple[bytes, datetime], bytes]:
        """Works like the regular :meth:`.Signer.unsign` but can also
        validate the time. See the base docstring of the class for
        the general behavior. If ``return_timestamp`` is ``True`` the
        timestamp of the signature will be returned as an aware
        :class:`datetime.datetime` object in UTC.

        .. versionchanged:: 2.0
            The timestamp is returned as a timezone-aware ``datetime``
            in UTC rather than a naive ``datetime`` assumed to be UTC.
        """
        try:
            result = super().unsign(signed_value)
            sig_error = None
        except BadSignature as e:
            sig_error = e
            result = e.payload or b""

        sep = want_bytes(self.sep)

        # If there is no timestamp in the result there is something
        # seriously wrong. In case there was a signature error, we raise
        # that one directly, otherwise we have a weird situation in
        # which we shouldn't have come except someone uses a time-based
        # serializer on non-timestamp data, so catch that.
        if sep not in result:
            if sig_error:
                raise sig_error

            raise BadTimeSignature("timestamp missing", payload=result)

        value, ts_bytes = result.rsplit(sep, 1)
        ts_int: _t_opt_int = None
        ts_dt: _t.Optional[datetime] = None

        try:
            ts_int = bytes_to_int(base64_decode(ts_bytes))
        except Exception:
            pass

        # Signature is *not* okay. Raise a proper error now that we have
        # split the value and the timestamp.
        if sig_error is not None:
            if ts_int is not None:
                ts_dt = self.timestamp_to_datetime(ts_int)

            raise BadTimeSignature(str(sig_error), payload=value, date_signed=ts_dt)

        # Signature was okay but the timestamp is actually not there or
        # malformed. Should not happen, but we handle it anyway.
        if ts_int is None:
            raise BadTimeSignature("Malformed timestamp", payload=value)

        # Check timestamp is not older than max_age
        if max_age is not None:
            age = self.get_timestamp() - ts_int

            if age > max_age:
                raise SignatureExpired(
                    f"Signature age {age} > {max_age} seconds",
                    payload=value,
                    date_signed=self.timestamp_to_datetime(ts_int),
                )

            if age < 0:
                raise SignatureExpired(
                    f"Signature age {age} < 0 seconds",
                    payload=value,
                    date_signed=self.timestamp_to_datetime(ts_int),
                )

        if return_timestamp:
            return value, self.timestamp_to_datetime(ts_int)

        return value

    def validate(self, signed_value: _t_str_bytes, max_age: _t_opt_int = None) -> bool:
        """Only validates the given signed value. Returns ``True`` if
        the signature exists and is valid."""
        try:
            self.unsign(signed_value, max_age=max_age)
            return True
        except BadSignature:
            return False


class TimedSerializer(Serializer):
    """Uses :class:`TimestampSigner` instead of the default
    :class:`.Signer`.
    """

    default_signer: _t.Type[TimestampSigner] = TimestampSigner

    def iter_unsigners(
        self, salt: _t_opt_str_bytes = None
    ) -> _t.Iterator[TimestampSigner]:
        return _t.cast("_t.Iterator[TimestampSigner]", super().iter_unsigners(salt))

    # TODO: Signature is incompatible because parameters were added
    #  before salt.

    def loads(  # type: ignore
        self,
        s: _t_str_bytes,
        max_age: _t_opt_int = None,
        return_timestamp: bool = False,
        salt: _t_opt_str_bytes = None,
    ) -> _t.Any:
        """Reverse of :meth:`dumps`, raises :exc:`.BadSignature` if the
        signature validation fails. If a ``max_age`` is provided it will
        ensure the signature is not older than that time in seconds. In
        case the signature is outdated, :exc:`.SignatureExpired` is
        raised. All arguments are forwarded to the signer's
        :meth:`~TimestampSigner.unsign` method.
        """
        s = want_bytes(s)
        last_exception = None

        for signer in self.iter_unsigners(salt):
            try:
                base64d, timestamp = signer.unsign(
                    s, max_age=max_age, return_timestamp=True
                )
                payload = self.load_payload(base64d)

                if return_timestamp:
                    return payload, timestamp

                return payload
            except SignatureExpired:
                # The signature was unsigned successfully but was
                # expired. Do not try the next signer.
                raise
            except BadSignature as err:
                last_exception = err

        raise _t.cast(BadSignature, last_exception)

    def loads_unsafe(  # type: ignore
        self,
        s: _t_str_bytes,
        max_age: _t_opt_int = None,
        salt: _t_opt_str_bytes = None,
    ) -> _t.Tuple[bool, _t.Any]:
        return self._loads_unsafe_impl(s, salt, load_kwargs={"max_age": max_age})
