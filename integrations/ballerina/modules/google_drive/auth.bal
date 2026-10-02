// Google Drive authentication settings and the flows that build an authenticated client.
//
// The user picks a flow by `authType`. Following the connector runtime's extensibility
// style, each flow is a named implementation of the `DriveAuthFlow` interface selected by
// the `newAuthFlow` factory — the single extension point — rather than an inline `if` in
// the client builder. Adding a flow later is one new class plus one branch in `newAuthFlow`.
//
// The `googleapis.drive` client's auth is only a bearer token or a refresh-token grant (it
// has no native JWT/service-account grant), so the service-account flow mints an access
// token itself from a signed JWT assertion and supplies it as a bearer. Because a bearer
// expires and the client will not refresh it, each flow also reports how long its client
// stays valid; the poll connector rebuilds the client when that runs out.

import ballerina/http;
import ballerina/jwt;
import ballerina/url;

import ballerinax/googleapis.drive as drive;

# Google's OAuth2 token endpoint, used by the refresh-token and service-account flows.
const string TOKEN_URL = "https://oauth2.googleapis.com/token";

# Read-only Drive scope: enough to list files, read changes, and download content.
const string DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";

# JWT bearer grant type for the service-account token exchange.
const string JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer";

# Validity for a self-refreshing or static credential: long enough that its client is never
# rebuilt on account of expiry (about ten years, in seconds).
const decimal NON_EXPIRING = 315360000;

# A minted service-account token lives one hour; rebuild a little before that to stay ahead
# of clock skew.
const decimal SERVICE_ACCOUNT_VALIDITY = 3300;

# Discriminator values for `GoogleDriveAuth`.
public const REFRESH_TOKEN = "refresh_token";
public const BEARER = "bearer";
public const SERVICE_ACCOUNT = "service_account";

# OAuth2 refresh-token flow: a refresh token is exchanged for access tokens, which the client
# refreshes on its own. The recommended flow for a long-running sync.
public type RefreshTokenAuth record {|
    # Flow discriminator.
    REFRESH_TOKEN authType = REFRESH_TOKEN;
    # OAuth2 client id.
    string clientId;
    # OAuth2 client secret.
    string clientSecret;
    # OAuth2 refresh token.
    string refreshToken;
|};

# A pre-obtained OAuth2 access token used directly. Static: it is not refreshed, so it stops
# working when it expires. Useful for short-lived tests.
public type BearerAuth record {|
    # Flow discriminator.
    BEARER authType = BEARER;
    # The OAuth2 access token.
    string token;
|};

# Service-account flow (JWT bearer): a signed assertion is exchanged for an access token.
# `privateKeyPath` is the path to the service account's private key in PEM form (the
# `private_key` from its JSON key, saved to a file). Set `subject` to impersonate a user
# under domain-wide delegation.
public type ServiceAccountAuth record {|
    # Flow discriminator.
    SERVICE_ACCOUNT authType = SERVICE_ACCOUNT;
    # Service account email (the JWT issuer).
    string clientEmail;
    # Path to the service account's private key in PEM form.
    string privateKeyPath;
    # Optional user to impersonate (domain-wide delegation).
    string subject?;
|};

# The Google Drive authentication settings for a connector instance. One of the supported
# flows, selected by `authType`.
public type GoogleDriveAuth RefreshTokenAuth|BearerAuth|ServiceAccountAuth;

# A pluggable Drive authentication flow. It builds an authenticated client and reports how
# long that client stays usable before it must be rebuilt.
public type DriveAuthFlow object {
    # Build a Drive client authenticated by this flow.
    #
    # + return - an authenticated client, or an error
    public function buildClient() returns drive:Client|error;

    # Seconds the built client stays valid. Self-refreshing or static credentials return a
    # very large value; the service-account flow returns its minted token's lifetime.
    #
    # + return - the client's validity in seconds
    public function validitySeconds() returns decimal;
};

# Select the authentication flow for a set of settings. The single extension point: a new
# flow is one new implementation plus one branch here.
#
# + auth - the connector instance's auth settings
# + return - the matching flow, or an error
public function newAuthFlow(GoogleDriveAuth auth) returns DriveAuthFlow|error {
    if auth is RefreshTokenAuth {
        return new RefreshTokenFlow(auth);
    }
    if auth is BearerAuth {
        return new BearerFlow(auth);
    }
    return new ServiceAccountFlow(auth);
}

# Builds clients from an OAuth2 refresh-token grant; the client refreshes access tokens itself.
class RefreshTokenFlow {
    *DriveAuthFlow;

    private final RefreshTokenAuth auth;

    function init(RefreshTokenAuth auth) {
        self.auth = auth;
    }

    public function buildClient() returns drive:Client|error {
        return new ({
            auth: {
                refreshUrl: TOKEN_URL,
                refreshToken: self.auth.refreshToken,
                clientId: self.auth.clientId,
                clientSecret: self.auth.clientSecret,
                scopes: [DRIVE_SCOPE]
            }
        });
    }

    public function validitySeconds() returns decimal => NON_EXPIRING;
}

# Builds clients from a static bearer token.
class BearerFlow {
    *DriveAuthFlow;

    private final BearerAuth auth;

    function init(BearerAuth auth) {
        self.auth = auth;
    }

    public function buildClient() returns drive:Client|error {
        return new ({auth: {token: self.auth.token}});
    }

    public function validitySeconds() returns decimal => NON_EXPIRING;
}

# Builds clients from a service-account key: mints an access token from a signed JWT
# assertion and supplies it as a bearer. Rebuilt near the token's expiry by the caller.
class ServiceAccountFlow {
    *DriveAuthFlow;

    private final ServiceAccountAuth auth;

    function init(ServiceAccountAuth auth) {
        self.auth = auth;
    }

    public function buildClient() returns drive:Client|error {
        string token = check self.mintAccessToken();
        return new ({auth: {token}});
    }

    public function validitySeconds() returns decimal => SERVICE_ACCOUNT_VALIDITY;

    // Sign a JWT assertion with the service account key and exchange it for an access token.
    private function mintAccessToken() returns string|error {
        jwt:IssuerConfig issuerConfig = {
            issuer: self.auth.clientEmail,
            audience: TOKEN_URL,
            expTime: 3600,
            customClaims: {"scope": DRIVE_SCOPE},
            signatureConfig: {
                algorithm: jwt:RS256,
                config: {keyFile: self.auth.privateKeyPath}
            }
        };
        string? subject = self.auth?.subject;
        if subject is string {
            // Domain-wide delegation impersonates this user.
            issuerConfig.customClaims["sub"] = subject;
        }
        string assertion = check jwt:issue(issuerConfig);

        string form = "grant_type=" + check url:encode(JWT_BEARER_GRANT, "UTF-8") +
                "&assertion=" + check url:encode(assertion, "UTF-8");
        http:Client tokenClient = check new ("https://oauth2.googleapis.com");
        http:Request request = new;
        request.setTextPayload(form, "application/x-www-form-urlencoded");
        json response = check tokenClient->post("/token", request);
        return (check response.access_token).toString();
    }
}
