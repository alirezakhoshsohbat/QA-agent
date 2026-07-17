@layer-bff
Feature: User Authentication Login
  As a registered user
  I want to authenticate with email and password
  So that I can access my account securely

  @acceptance-AC-1 @doc-auth
  Scenario: Successful login issues token cookies
    Given a registered user with email "user@example.com" and password "correct-horse"
    When the client sends POST /api/auth/login with body { "email": "user@example.com", "password": "correct-horse" }
    Then the response status is 200
    And the response sets HTTP-only cookies "access_token" and "refresh_token"

  @acceptance-AC-2 @doc-auth
  Scenario: Login rejects an invalid password
    Given a registered user with email "user@example.com"
    When the client sends POST /api/auth/login with body { "email": "user@example.com", "password": "wrong" }
    Then the response status is 401
    And the response body contains { "error": "INVALID_CREDENTIALS" }

  @acceptance-AC-3 @doc-auth
  Scenario: Refresh rotates the refresh token
    Given a valid "refresh_token" cookie is present
    When the client sends POST /api/auth/refresh
    Then the response status is 200
    And a new "refresh_token" cookie is set and the previous token is revoked

  @acceptance-AC-4 @doc-auth
  Scenario: Logout clears session cookies
    Given an authenticated session with an "access_token" cookie
    When the client sends POST /api/auth/logout
    Then the response status is 204
    And cookies "access_token" and "refresh_token" are cleared with Max-Age=0
