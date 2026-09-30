// Tests for Salesforce auth decoding and the mapping onto the library config types.

import ballerina/http;
import ballerina/oauth2;
import ballerina/test;

const BASE = "https://example.my.salesforce.com";
const TOKEN_URL = "https://example.my.salesforce.com/services/oauth2/token";

// A settings JSON decodes into the matching SalesforceAuth member by `authType`.
@test:Config
function testClientCredentialsDecodes() returns error? {
    json config = {
        auth: {authType: "client_credentials", clientId: "cid", clientSecret: "secret"},
        baseUrl: BASE,
        sobject: "Account",
        fields: ["Name"]
    };
    SalesforceSettings s = check config.cloneWithType();
    test:assertTrue(s.auth is ClientCredentialsAuth);
}

@test:Config
function testRefreshTokenDecodes() returns error? {
    json config = {
        auth: {authType: "refresh_token", clientId: "cid", clientSecret: "secret", refreshToken: "rt"},
        baseUrl: BASE,
        sobject: "Account",
        fields: ["Name"]
    };
    SalesforceSettings s = check config.cloneWithType();
    test:assertTrue(s.auth is RefreshTokenAuth);
}

@test:Config
function testBearerDecodes() returns error? {
    json config = {
        auth: {authType: "bearer", token: "abc"},
        baseUrl: BASE,
        sobject: "Account",
        fields: ["Name"]
    };
    SalesforceSettings s = check config.cloneWithType();
    test:assertTrue(s.auth is BearerAuth);
}

// The client-config builder maps each flow to the right library type, deriving the
// token/refresh URL from the base URL.
@test:Config
function testClientAuthConfigMapping() {
    var cc = clientAuthConfig({clientId: "cid", clientSecret: "secret"}, BASE);
    test:assertTrue(cc is http:OAuth2ClientCredentialsGrantConfig);
    if cc is http:OAuth2ClientCredentialsGrantConfig {
        test:assertEquals(cc.tokenUrl, TOKEN_URL);
    }

    var rt = clientAuthConfig({authType: REFRESH_TOKEN, clientId: "c", clientSecret: "s", refreshToken: "rt"}, BASE);
    test:assertTrue(rt is http:OAuth2RefreshTokenGrantConfig);
    if rt is http:OAuth2RefreshTokenGrantConfig {
        test:assertEquals(rt.refreshUrl, TOKEN_URL);
    }

    var bearer = clientAuthConfig({authType: BEARER, token: "tok"}, BASE);
    test:assertTrue(bearer is http:BearerTokenConfig);
}

// The listener-config builder maps each flow to the listener's oauth2 variants.
@test:Config
function testListenerAuthConfigMapping() {
    var cc = listenerAuthConfig({clientId: "cid", clientSecret: "secret"}, BASE);
    test:assertTrue(cc is oauth2:ClientCredentialsGrantConfig);

    var rt = listenerAuthConfig({authType: REFRESH_TOKEN, clientId: "c", clientSecret: "s", refreshToken: "rt"}, BASE);
    test:assertTrue(rt is oauth2:RefreshTokenGrantConfig);

    var bearer = listenerAuthConfig({authType: BEARER, token: "tok"}, BASE);
    test:assertTrue(bearer is http:BearerTokenConfig);
}
