// Salesforce authentication settings and the mapping onto the library's config types.
//
// The Salesforce REST `Client` and CDC `Listener` both accept the same auth flows,
// but expose them through slightly different config types: the client takes
// `http:OAuth2*GrantConfig`, the listener takes `salesforce:OAuth2Config` (the
// `oauth2:*GrantConfig` variants). This module defines one provider-neutral
// `SalesforceAuth` union and the two builders that map it onto each API, so the
// poll and listen connectors are configured identically from a single `auth` block.

import ballerina/http;
import ballerina/oauth2;

import ballerinax/salesforce;

# Discriminator values for `SalesforceAuth`.
public const CLIENT_CREDENTIALS = "client_credentials";
public const REFRESH_TOKEN = "refresh_token";
public const BEARER = "bearer";

# OAuth2 client-credentials flow: server-to-server, no user and no refresh token.
# Enable it on the Connected App and set a run-as user. Immune to refresh-token
# rotation because no refresh token is involved.
public type ClientCredentialsAuth record {|
    # Flow discriminator.
    CLIENT_CREDENTIALS authType = CLIENT_CREDENTIALS;
    # Connected App consumer key.
    string clientId;
    # Connected App consumer secret.
    string clientSecret;
|};

# OAuth2 refresh-token flow: a refresh token is exchanged for access tokens. Rotated
# tokens are cached in memory (single replica only). The `refreshToken` is a seed
# reused on restart, and the poll and listen contexts each hold it, so under strict
# Refresh Token Rotation prefer client-credentials.
public type RefreshTokenAuth record {|
    # Flow discriminator.
    REFRESH_TOKEN authType = REFRESH_TOKEN;
    # Connected App consumer key.
    string clientId;
    # Connected App consumer secret.
    string clientSecret;
    # The OAuth2 refresh token (seed; rotated in memory thereafter).
    string refreshToken;
|};

# A pre-obtained OAuth2 access token used directly as a bearer credential. Simplest
# to configure but the token is static: it is not refreshed, so it stops working
# when it expires. Useful for short-lived tests.
public type BearerAuth record {|
    # Flow discriminator.
    BEARER authType = BEARER;
    # The OAuth2 access token.
    string token;
|};

# The Salesforce authentication settings for a connector instance. One of the
# supported flows, selected by `authType`.
public type SalesforceAuth ClientCredentialsAuth|RefreshTokenAuth|BearerAuth;

// The client-credentials token endpoint and the refresh endpoint are both the
// instance's My Domain OAuth token URL.
isolated function tokenEndpoint(string baseUrl) returns string {
    return baseUrl + "/services/oauth2/token";
}

// Map `SalesforceAuth` onto the REST client's auth config.
isolated function clientAuthConfig(SalesforceAuth auth, string baseUrl)
        returns http:BearerTokenConfig|http:OAuth2RefreshTokenGrantConfig|
        http:OAuth2ClientCredentialsGrantConfig {
    string tokenUrl = tokenEndpoint(baseUrl);
    if auth is ClientCredentialsAuth {
        return {tokenUrl, clientId: auth.clientId, clientSecret: auth.clientSecret};
    }
    if auth is RefreshTokenAuth {
        return {
            refreshUrl: tokenUrl,
            refreshToken: auth.refreshToken,
            clientId: auth.clientId,
            clientSecret: auth.clientSecret
        };
    }
    return {token: auth.token};
}

// Map `SalesforceAuth` onto the CDC listener's auth config. Structurally identical
// to `clientAuthConfig` but typed for the listener's `oauth2:*` variants.
isolated function listenerAuthConfig(SalesforceAuth auth, string baseUrl)
        returns salesforce:OAuth2Config {
    string tokenUrl = tokenEndpoint(baseUrl);
    if auth is ClientCredentialsAuth {
        oauth2:ClientCredentialsGrantConfig config = {
            tokenUrl,
            clientId: auth.clientId,
            clientSecret: auth.clientSecret
        };
        return config;
    }
    if auth is RefreshTokenAuth {
        oauth2:RefreshTokenGrantConfig config = {
            refreshUrl: tokenUrl,
            refreshToken: auth.refreshToken,
            clientId: auth.clientId,
            clientSecret: auth.clientSecret
        };
        return config;
    }
    http:BearerTokenConfig config = {token: auth.token};
    return config;
}
