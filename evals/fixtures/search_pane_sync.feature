@layer-frontend
Feature: Search Pane Frontend Sync
  As a user
  I want filter chips to reflect the agent's visible filter list
  So that my search criteria stay in sync

  @acceptance-E-1 @doc-f57d80cf
  Scenario: Init renders only agent-listed chips hydrated from localStorage
    Given GET /api/bot/init returns data.filters = ["propertyType", "rooms"]
    And localStorage['searchCriteria'] = { "propertyType": ["condo"], "rooms": "3+" }
    When the Search Pane renders
    Then only the propertyType and rooms chips are visible
    And the rooms chip shows "3+"

@layer-bff
Feature: Search Pane BFF Send Message
  As the BFF
  I want to forward only message content upstream
  So that filter values never leak onto the message payload

  @acceptance-B-1 @doc-f8e00124
  Scenario: Valid filters update profile then send content only
    Given POST /api/bot/send-message with body { "message": "hi", "filters": { "rooms": "3+" } }
    When the BFF issues PATCH /v1/profile and it returns 200
    Then the upstream send-message body equals { "content": "hi" }
