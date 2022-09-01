# Copyright (c) 2022, Oracle and/or its affiliates.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2.0, as
# published by the Free Software Foundation.
#
# This program is also distributed with certain software (including
# but not limited to OpenSSL) that is licensed under separate terms,
# as designated in a particular file or component or in included license
# documentation.  The authors of MySQL hereby grant you an
# additional permission to link the program and your derivative works
# with the separately licensed software that they have included with
# MySQL.
#
# Without limiting anything contained in the foregoing, this file,
# which is part of MySQL Connector/Python, is also subject to the
# Universal FOSS Exception, version 1.0, a copy of which can be found at
# http://oss.oracle.com/licenses/universal-foss-exception.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License, version 2.0, for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA

# mypy: disable-error-code="str-bytes-safe,misc"

"""Kerberos Authentication Plugin."""

import getpass
import logging
import os
import struct

from pathlib import Path
from typing import Any, Optional, Tuple

from .. import errors

try:
    import gssapi
except ImportError:
    gssapi = None
    if os.name != "nt":
        raise errors.ProgrammingError(
            "Module gssapi is required for GSSAPI authentication "
            "mechanism but was not found. Unable to authenticate "
            "with the server"
        ) from None

try:
    import sspi
    import sspicon
except ImportError:
    sspi = None
    sspicon = None

from . import BaseAuthPlugin

logging.getLogger(__name__).addHandler(logging.NullHandler())

_LOGGER = logging.getLogger(__name__)

AUTHENTICATION_PLUGIN_CLASS = (
    "MySQLSSPIKerberosAuthPlugin" if os.name == "nt" else "MySQLKerberosAuthPlugin"
)


# pylint: disable=c-extension-no-member,no-member
class MySQLKerberosAuthPlugin(BaseAuthPlugin):
    """Implement the MySQL Kerberos authentication plugin."""

    plugin_name: str = "authentication_kerberos_client"
    requires_ssl: bool = False
    context: Optional[gssapi.SecurityContext] = None

    @staticmethod
    def get_user_from_credentials() -> str:
        """Get user from credentials without realm."""
        try:
            creds = gssapi.Credentials(usage="initiate")
            user = str(creds.name)
            if user.find("@") != -1:
                user, _ = user.split("@", 1)
            return user
        except gssapi.raw.misc.GSSError:
            return getpass.getuser()

    @staticmethod
    def get_store() -> dict:
        """Get a credentials store dictionary.

        Returns:
            dict: Credentials store dictionary with the krb5 ccache name.

        Raises:
            errors.InterfaceError: If 'KRB5CCNAME' environment variable is empty.
        """
        krb5ccname = os.environ.get(
            "KRB5CCNAME",
            f"/tmp/krb5cc_{os.getuid()}"
            if os.name == "posix"
            else Path("%TEMP%").joinpath("krb5cc"),
        )
        if not krb5ccname:
            raise errors.InterfaceError(
                "The 'KRB5CCNAME' environment variable is set to empty"
            )
        _LOGGER.debug("Using krb5 ccache name: FILE:%s", krb5ccname)
        store = {b"ccache": f"FILE:{krb5ccname}".encode("utf-8")}
        return store

    def _acquire_cred_with_password(self, upn: str) -> gssapi.raw.creds.Creds:
        """Acquire and store credentials through provided password.

        Args:
            upn (str): User Principal Name.

        Returns:
            gssapi.raw.creds.Creds: GSSAPI credentials.
        """
        _LOGGER.debug("Attempt to acquire credentials through provided password")
        user = gssapi.Name(upn, gssapi.NameType.user)
        password = self._password.encode("utf-8")

        try:
            acquire_cred_result = gssapi.raw.acquire_cred_with_password(
                user, password, usage="initiate"
            )
            creds = acquire_cred_result.creds
            gssapi.raw.store_cred_into(
                self.get_store(),
                creds=creds,
                mech=gssapi.MechType.kerberos,
                overwrite=True,
                set_default=True,
            )
        except gssapi.raw.misc.GSSError as err:
            raise errors.ProgrammingError(
                f"Unable to acquire credentials with the given password: {err}"
            )
        return creds

    @staticmethod
    def _parse_auth_data(packet: bytes) -> Tuple[str, str]:
        """Parse authentication data.

        Get the SPN and REALM from the authentication data packet.

        Format:
            SPN string length two bytes <B1> <B2> +
            SPN string +
            UPN realm string length two bytes <B1> <B2> +
            UPN realm string

        Returns:
            tuple: With 'spn' and 'realm'.
        """
        spn_len = struct.unpack("<H", packet[:2])[0]
        packet = packet[2:]

        spn = struct.unpack(f"<{spn_len}s", packet[:spn_len])[0]
        packet = packet[spn_len:]

        realm_len = struct.unpack("<H", packet[:2])[0]
        realm = struct.unpack(f"<{realm_len}s", packet[2:])[0]

        return spn.decode(), realm.decode()

    def auth_response(self, auth_data: Optional[bytes] = None) -> Optional[bytes]:
        """Prepare the first message to the server."""
        spn = None
        realm = None

        if auth_data:
            try:
                spn, realm = self._parse_auth_data(auth_data)
            except struct.error as err:
                raise InterruptedError(f"Invalid authentication data: {err}") from err

        if spn is None:
            return self.prepare_password()

        upn = f"{self._username}@{realm}" if self._username else None

        _LOGGER.debug("Service Principal: %s", spn)
        _LOGGER.debug("Realm: %s", realm)
        _LOGGER.debug("Username: %s", self._username)

        try:
            # Attempt to retrieve credentials from cache file
            creds: Any = gssapi.Credentials(usage="initiate")
            creds_upn = str(creds.name)

            _LOGGER.debug("Cached credentials found")
            _LOGGER.debug("Cached credentials UPN: %s", creds_upn)

            # Remove the realm from user
            if creds_upn.find("@") != -1:
                creds_user, creds_realm = creds_upn.split("@", 1)
            else:
                creds_user = creds_upn
                creds_realm = None

            upn = f"{self._username}@{realm}" if self._username else creds_upn

            # The user from cached credentials matches with the given user?
            if self._username and self._username != creds_user:
                _LOGGER.debug(
                    "The user from cached credentials doesn't match with the "
                    "given user"
                )
                if self._password is not None:
                    creds = self._acquire_cred_with_password(upn)
            if creds_realm and creds_realm != realm and self._password is not None:
                creds = self._acquire_cred_with_password(upn)
        except gssapi.raw.exceptions.ExpiredCredentialsError as err:
            if upn and self._password is not None:
                creds = self._acquire_cred_with_password(upn)
            else:
                raise errors.InterfaceError(f"Credentials has expired: {err}")
        except gssapi.raw.misc.GSSError as err:
            if upn and self._password is not None:
                creds = self._acquire_cred_with_password(upn)
            else:
                raise errors.InterfaceError(
                    f"Unable to retrieve cached credentials error: {err}"
                )

        flags = (
            gssapi.RequirementFlag.mutual_authentication,
            gssapi.RequirementFlag.extended_error,
            gssapi.RequirementFlag.delegate_to_peer,
        )
        name = gssapi.Name(spn, name_type=gssapi.NameType.kerberos_principal)
        cname = name.canonicalize(gssapi.MechType.kerberos)
        self.context = gssapi.SecurityContext(
            name=cname, creds=creds, flags=sum(flags), usage="initiate"
        )

        try:
            initial_client_token: Optional[bytes] = self.context.step()
        except gssapi.raw.misc.GSSError as err:
            raise errors.InterfaceError(f"Unable to initiate security context: {err}")

        _LOGGER.debug("Initial client token: %s", initial_client_token)
        return initial_client_token

    def auth_continue(
        self, tgt_auth_challenge: Optional[bytes]
    ) -> Tuple[Optional[bytes], bool]:
        """Continue with the Kerberos TGT service request.

        With the TGT authentication service given response generate a TGT
        service request. This method must be invoked sequentially (in a loop)
        until the security context is completed and an empty response needs to
        be send to acknowledge the server.

        Args:
            tgt_auth_challenge: the challenge for the negotiation.

        Returns:
            tuple (bytearray TGS service request,
            bool True if context is completed otherwise False).
        """
        _LOGGER.debug("tgt_auth challenge: %s", tgt_auth_challenge)

        resp: Optional[bytes] = self.context.step(tgt_auth_challenge)

        _LOGGER.debug("Context step response: %s", resp)
        _LOGGER.debug("Context completed?: %s", self.context.complete)

        return resp, self.context.complete

    def auth_accept_close_handshake(self, message: bytes) -> bytes:
        """Accept handshake and generate closing handshake message for server.

        This method verifies the server authenticity from the given message
        and included signature and generates the closing handshake for the
        server.

        When this method is invoked the security context is already established
        and the client and server can send GSSAPI formated secure messages.

        To finish the authentication handshake the server sends a message
        with the security layer availability and the maximum buffer size.

        Since the connector only uses the GSSAPI authentication mechanism to
        authenticate the user with the server, the server will verify clients
        message signature and terminate the GSSAPI authentication and send two
        messages; an authentication acceptance b'\x01\x00\x00\x08\x01' and a
        OK packet (that must be received after sent the returned message from
        this method).

        Args:
            message: a wrapped gssapi message from the server.

        Returns:
            bytearray (closing handshake message to be send to the server).
        """
        if not self.context.complete:
            raise errors.ProgrammingError("Security context is not completed")
        _LOGGER.debug("Server message: %s", message)
        _LOGGER.debug("GSSAPI flags in use: %s", self.context.actual_flags)
        try:
            unwraped = self.context.unwrap(message)
            _LOGGER.debug("Unwraped: %s", unwraped)
        except gssapi.raw.exceptions.BadMICError as err:
            _LOGGER.debug("Unable to unwrap server message: %s", err)
            raise errors.InterfaceError(f"Unable to unwrap server message: {err}")

        _LOGGER.debug("Unwrapped server message: %s", unwraped)
        # The message contents for the clients closing message:
        #   - security level 1 byte, must be always 1.
        #   - conciliated buffer size 3 bytes, without importance as no
        #     further GSSAPI messages will be sends.
        response = bytearray(b"\x01\x00\x00\00")
        # Closing handshake must not be encrypted.
        _LOGGER.debug("Message response: %s", response)
        wraped = self.context.wrap(response, encrypt=False)
        _LOGGER.debug(
            "Wrapped message response: %s, length: %d",
            wraped[0],
            len(wraped[0]),
        )

        return wraped.message


class MySQLSSPIKerberosAuthPlugin(BaseAuthPlugin):
    """Implement the MySQL Kerberos authentication plugin with Windows SSPI"""

    plugin_name: str = "authentication_kerberos_client"
    requires_ssl: bool = False
    context: Any = None
    clientauth: Any = None

    @staticmethod
    def _parse_auth_data(packet: bytes) -> Tuple[str, str]:
        """Parse authentication data.

        Get the SPN and REALM from the authentication data packet.

        Format:
            SPN string length two bytes <B1> <B2> +
            SPN string +
            UPN realm string length two bytes <B1> <B2> +
            UPN realm string

        Returns:
            tuple: With 'spn' and 'realm'.
        """
        spn_len = struct.unpack("<H", packet[:2])[0]
        packet = packet[2:]

        spn = struct.unpack(f"<{spn_len}s", packet[:spn_len])[0]
        packet = packet[spn_len:]

        realm_len = struct.unpack("<H", packet[:2])[0]
        realm = struct.unpack(f"<{realm_len}s", packet[2:])[0]

        return spn.decode(), realm.decode()

    def auth_response(self, auth_data: Optional[bytes] = None) -> Optional[bytes]:
        """Prepare the first message to the server."""
        _LOGGER.debug("auth_response for sspi")
        spn = None
        realm = None

        if auth_data:
            try:
                spn, realm = self._parse_auth_data(auth_data)
            except struct.error as err:
                raise InterruptedError(f"Invalid authentication data: {err}") from err

        _LOGGER.debug("Service Principal: %s", spn)
        _LOGGER.debug("Realm: %s", realm)
        _LOGGER.debug("Username: %s", self._username)

        if sspicon is None or sspi is None:
            raise errors.ProgrammingError(
                'Package "pywin32" (Python for Win32 (pywin32) extensions)'
                " is not installed."
            )

        flags = (sspicon.ISC_REQ_MUTUAL_AUTH, sspicon.ISC_REQ_DELEGATE)

        if self._username and self._password:
            _auth_info = (self._username, realm, self._password)
        else:
            _auth_info = None

        targetspn = spn
        _LOGGER.debug("targetspn: %s", targetspn)
        _LOGGER.debug("_auth_info is None: %s", _auth_info is None)

        # The Security Support Provider Interface (SSPI) is an interface
        # that allows us to choose from a set of SSPs available in the
        # system; the idea of SSPI is to keep interface consistent no
        # matter what back end (a.k.a., SSP) we choose.

        # When using SSPI we should not use Kerberos directly as SSP,
        # as remarked in [2], but we can use it indirectly via another
        # SSP named Negotiate that acts as an application layer between
        # SSPI and the other SSPs [1].

        # Negotiate can select between Kerberos and NTLM on the fly;
        # it chooses Kerberos unless it cannot be used by one of the
        # systems involved in the authentication or the calling
        # application did not provide sufficient information to use
        # Kerberos.

        # [1] https://docs.microsoft.com/en-us/windows/win32/secauthn/microsoft-negotiate?source=recommendations
        # [2] https://docs.microsoft.com/en-us/windows/win32/secauthn/microsoft-kerberos?source=recommendations
        self.clientauth = sspi.ClientAuth(
            "Negotiate",
            targetspn=targetspn,
            auth_info=_auth_info,
            scflags=sum(flags),
            datarep=sspicon.SECURITY_NETWORK_DREP,
        )

        try:
            data = None
            err, out_buf = self.clientauth.authorize(data)
            _LOGGER.debug("Context step err: %s", err)
            _LOGGER.debug("Context step out_buf: %s", out_buf)
            _LOGGER.debug("Context completed?: %s", self.clientauth.authenticated)
            initial_client_token = out_buf[0].Buffer
            _LOGGER.debug("pkg_info: %s", self.clientauth.pkg_info)
        except Exception as err:
            raise errors.InterfaceError(
                f"Unable to initiate security context: {err}"
            ) from err

        _LOGGER.debug("Initial client token: %s", initial_client_token)
        return initial_client_token

    def auth_continue(
        self, tgt_auth_challenge: Optional[bytes]
    ) -> Tuple[Optional[bytes], bool]:
        """Continue with the Kerberos TGT service request.

        With the TGT authentication service given response generate a TGT
        service request. This method must be invoked sequentially (in a loop)
        until the security context is completed and an empty response needs to
        be send to acknowledge the server.

        Args:
            tgt_auth_challenge: the challenge for the negotiation.

        Returns:
            tuple (bytearray TGS service request,
            bool True if context is completed otherwise False).
        """
        _LOGGER.debug("tgt_auth challenge: %s", tgt_auth_challenge)

        err, out_buf = self.clientauth.authorize(tgt_auth_challenge)

        _LOGGER.debug("Context step err: %s", err)
        _LOGGER.debug("Context step out_buf: %s", out_buf)
        resp = out_buf[0].Buffer
        _LOGGER.debug("Context step resp: %s", resp)
        _LOGGER.debug("Context completed?: %s", self.clientauth.authenticated)

        return resp, self.clientauth.authenticated
