"""
Custom Authenticator to use OpenShift OAuth with JupyterHub.

Derived from the GitHub OAuth authenticator.
"""
import os

import requests
from jupyterhub.auth import LocalAuthenticator
from tornado.curl_httpclient import CurlError
from tornado.httpclient import HTTPRequest, HTTPError
from tornado.httputil import url_concat
from traitlets import Bool
from traitlets import default
from traitlets import Set
from traitlets import Unicode

from oauthenticator.oauth2 import OAuthenticator


class OpenShiftOAuthenticator(OAuthenticator):

    login_service = "OpenShift"

    scope = ['user:info']

    openshift_url = Unicode(
        os.environ.get('OPENSHIFT_URL')
        or 'https://openshift.default.svc.cluster.local',
        config=True,
    )

    ca_certs = Unicode(config=True)
    system_ca_certs = Unicode(config=True)
    use_ca_certs_for_token_request = True if ca_certs else False

    allowed_groups = Set(
        config=True,
        help="Set of OpenShift groups that should be allowed to access the hub.",
    )

    admin_groups = Set(
        config=True,
        help="Set of OpenShift groups that should be given admin access to the hub.",
    )

    @default("ca_certs")
    def _ca_certs_default(self):
        ca_cert_file = "/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if self.validate_server_cert and os.path.exists(ca_cert_file):
            return ca_cert_file
        return ''

    @default("system_ca_certs")
    def _system_ca_certs_default(self):
        ca_cert_file = "/etc/pki/tls/cert.pem"
        if self.validate_server_cert and os.path.exists(ca_cert_file):
            return ca_cert_file
        return ''

    openshift_auth_api_url = Unicode(config=True)

    @default("openshift_auth_api_url")
    def _openshift_auth_api_url_default(self):
        auth_info_url = '%s/.well-known/oauth-authorization-server' % self.openshift_url

        resp = requests.get(auth_info_url, verify=self.ca_certs or self.validate_server_cert)
        resp_json = resp.json()

        return resp_json.get('issuer')

    openshift_rest_api_url = Unicode(
        os.environ.get('OPENSHIFT_REST_API_URL')
        or 'https://openshift.default.svc.cluster.local',
        config=True,
    )

    @default("openshift_rest_api_url")
    def _openshift_rest_api_url_default(self):
        return self.openshift_url

    @default("authorize_url")
    def _authorize_url_default(self):
        return "%s/oauth/authorize" % self.openshift_auth_api_url

    @default("token_url")
    def _token_url_default(self):
        return "%s/oauth/token" % self.openshift_auth_api_url

    @default("userdata_url")
    def _userdata_url_default(self):
        return "%s/apis/user.openshift.io/v1/users/~" % self.openshift_rest_api_url

    @staticmethod
    def user_in_groups(user_groups: set, allowed_groups: set):
        return any(user_groups.intersection(allowed_groups))

    async def authenticate(self, handler, data=None):
        code = handler.get_argument("code")

        # Exchange the OAuth code for a OpenShift Access Token
        #
        # See: https://docs.openshift.org/latest/architecture/additional_concepts/authentication.html#api-authentication

        params = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            grant_type="authorization_code",
            code=code,
        )

        url = url_concat(self.token_url, params)

        def token_request(url):
            return HTTPRequest(
                url,
                method="POST",
                validate_cert=self.validate_server_cert,
                ca_certs=self.ca_certs if self.use_ca_certs_for_token_request else self.system_ca_certs,
                headers={"Accept": "application/json"},
                body='',  # Body is required for a POST...
               )
        try:
            req = token_request(url)
            resp = await self.fetch(req)
        except CurlError:
            certs = "system ca certs" if self.use_ca_certs_for_token_request else "ca certs"
            self.log.info("Retrying oauth token request with %s" % certs)
            self.use_ca_certs_for_token_request = not self.use_ca_certs_for_token_request
            req = token_request(url)
            resp = await self.fetch(req)

        access_token = resp['access_token']
        user_info = await self._get_openshift_user_info(access_token)

        return user_info

    async def _get_openshift_user_info(self, access_token):

        # Determine who the logged in user is
        headers = {
            "Accept": "application/json",
            "User-Agent": "JupyterHub",
            "Authorization": "Bearer {}".format(access_token),
        }

        req = HTTPRequest(
            self.userdata_url,
            method="GET",
            validate_cert=self.validate_server_cert,
            ca_certs=self.ca_certs,
            headers=headers,
        )

        ocp_user = {}
        try:
            ocp_user = await self.fetch(req) #TODO: tornado.httpclient.HTTPClientError: HTTP 401: Unauthorized
        except HTTPError as ex:
            if ex.code == 401:
                return None

            raise ex

        username = ocp_user['metadata']['name']

        user_info = {
            'name': username,
            'auth_state': {'access_token': access_token, 'openshift_user': ocp_user},
        }

        if self.allowed_groups or self.admin_groups:
            user_info = await self._add_openshift_group_info(user_info)

        return user_info

    async def _add_openshift_group_info(self, user_info: dict):
        """
        Use the group info stored on the OpenShift User object to determine if a user
        is authenticated based on groups, an admin, or both.
        """
        user_groups = set(user_info['auth_state']['openshift_user']['groups'])
        username = user_info['name']

        if self.admin_groups:
            is_admin = self.user_in_groups(user_groups, self.admin_groups)

        user_in_allowed_group = self.user_in_groups(user_groups, self.allowed_groups)

        if self.admin_groups and (is_admin or user_in_allowed_group):
            user_info['admin'] = is_admin
            return user_info
        elif user_in_allowed_group:
            return user_info
        else:
            msg = f"username:{username} User not in any of the allowed/admin groups"
            self.log.warning(msg)
            return None

    async def refresh_user(self, user, handler=None):
        # Retrieve user authentication info, decode, and check if refresh is needed
        auth_state = await user.get_auth_state()

        user_info = await self._get_openshift_user_info(auth_state['access_token'])
        if not user_info:
            await user.stop()

        return user_info


class LocalOpenShiftOAuthenticator(LocalAuthenticator, OpenShiftOAuthenticator):

    """A version that mixes in local system user creation"""

    pass
