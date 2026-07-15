import unittest
from unittest.mock import patch, MagicMock
import sqlite3
# Import check_and_alert while mocking its dependencies to avoid global scope errors during test setup
from newshound import check_and_alert 

class TestNewshound(unittest.TestCase):

    def setUp(self):
        # Setup a mock connection object
        self.mock_conn = MagicMock()
        self.mock_curr = self.mock_conn.cursor().return_value
        self.mock_curr.fetchone.reset_mock()

    @patch('newshound.get_last_processed_guid_from_db', return_value='old_guid')
    @patch('newshound.feedparser.parse')
    @patch('newshound.update_last_processed_guid_in_db')
    @patch('newshound.slack_client') # Mock the entire slack client dependency passed to the test function
    @patch('os.environ', {'LOCAL_TESTING': 'True'}) # Force test mode printing
    def test_new_content_alerting(self, mock_env, MockSlackClient, mock_update, mock_feedparser, mock_get_guid):
        # Setup a mock entry list: one old, one new, one irrelevant
        mock_entry_1 = {'id': 'old_guid', 'title': 'Old News', 'link': 'http://example.com'}
        mock_entry_2 = {'id': 'new_guid', 'title': 'Crypto Surge!', 'link': 'http://crypto.com/surge'} # Matches keyword, new
        mock_entry_3 = {'id': 'random', 'title': 'Random Topic', 'link': 'http://other.net'} # Does not match keyword

        # Mock the parsed feed object to return entries
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry_1, mock_entry_2, mock_entry_3]
        mock_feedparser.return_value = mock_feed

        with patch('newshound.get_keywords', return_value=['crypto']):
             # Call the function with all necessary dependencies
            check_and_alert("testurl", self.mock_conn, MockSlackClient)

        # Assert that mocking occurred as expected
        mock_feedparser.assert_called_once_with("testurl")
        
        # Assert state update happened with the last found *new* GUID
        mock_update.assert_called_once_with(self.mock_conn, "testurl", 'new_guid')
        # Assert Slack was NOT called in test mode if local testing is active
        MockSlackClient.chat_postMessage.assert_not_called()


    @patch('newshound.get_last_processed_guid_from_db', return_value='final_state')
    @patch('newshound.feedparser.parse')
    @patch('newshound.update_last_processed_guid_in_db')
    @patch('newshound.slack_client')
    def test_no_new_content(self, MockSlackClient, mock_update, mock_feedparser, mock_get_guid):
        # Setup entries where all items are already processed (same GUID) or irrelevant.
        mock_entry_1 = {'id': 'final_state', 'title': 'Old 1', 'link': 'http://example.com'}
        mock_entry_2 = {'id': 'final_state', 'title': 'Old 2', 'link': 'http://example.com'}
        
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry_1, mock_entry_2]
        mock_feedparser.return_value = mock_feed

        with patch('newshound.get_keywords', return_value=['crypto']):
            check_and_alert("testurl", self.mock_conn, MockSlackClient)

        # Assert that no state update occurred if nothing new was found
        mock_update.assert_not_called()