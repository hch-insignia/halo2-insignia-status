"""Offline tests: no real Discord requests or credentials."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import halo2_feed as feed


def soup(text):
    return BeautifulSoup(text, "html.parser")


class FeedTests(unittest.TestCase):
    def test_halo_only_and_occupied_slots(self):
        document = soup('''<section id="games"><table><tbody><tr>
          <td><a href="https://insignia.live/games/4d530064">Halo 2</a></td>
          <td>MS-100</td><td>45</td><td><table>
          <tr title="Public Matches"><td>icon</td><td>21 / 32</td></tr>
          <tr title="Private Matches"><td>icon</td><td>9 / 16</td></tr>
          </table></td><td>features</td></tr></tbody></table></section>
          <h2>Popular this week</h2><table><tbody><tr>
          <td><a href="https://insignia.live/games/4d530064">Halo 2</a></td>
          <td>MS-100</td><td>1,234</td></tr></tbody></table>''')
        self.assertEqual(feed.parse_online(document), 45)
        self.assertEqual(feed.parse_in_game(document), 30)
        self.assertEqual(feed.parse_weekly(document), 1234)

    def test_active_only_and_no_mentions(self):
        document = soup('''<details name="playlist-accordion"><summary>
          <span>Big Team Battle</span><span>21 Players</span></summary></details>
          <details name="playlist-accordion"><summary>Solo Arena 9 Players</summary></details>
          <details name="playlist-accordion"><summary>Team SWAT 0 Players</summary></details>''')
        data = dict(online=45, in_game=30, weekly=1234,
                    best="8 PM–9 PM EST", playlists=feed.parse_playlists(document))
        body = feed.payload(data, datetime.now(feed.UTC))
        description = body['embeds'][0]['description']
        self.assertIn('Big Team Battle: **21**', description)
        self.assertIn('Solo Arena: **9**', description)
        self.assertNotIn('Team SWAT', description)
        self.assertEqual(body['allowed_mentions'], {'parse': []})

    def test_missing_source_is_not_zero(self):
        data = feed.snapshot(None, soup('<html>Maintenance</html>'), datetime.now(feed.UTC), ZoneInfo('America/New_York'))
        self.assertIsNone(data['online'])
        self.assertIsNone(data['playlists'])
        self.assertEqual(data['best'], 'Unavailable')

    def test_top_five_playlists_in_descending_order(self):
        data = dict(online=90, in_game=70, weekly=1234, best='8 PM–9 PM EST',
                    playlists={'Team SWAT': 8, 'Solo Arena': 9, 'Double Team': 2,
                               'Big Team Battle': 21, 'Multi-Team': 12,
                               'Team Slayer': 16, 'Team Snipers': 0})
        body = feed.payload(data, datetime.now(feed.UTC))['embeds'][0]['description']
        lines = body.split('**[Active Playlists](https://insignia.live/halo2):**\n')[1].split('\n\n')[0].splitlines()
        self.assertEqual(lines, ['- Big Team Battle: **21**', '- Team Slayer: **16**',
                                 '- Multi-Team: **12**', '- Solo Arena: **9**', '- Team SWAT: **8**'])

    def test_all_zero_is_legitimate(self):
        lists = feed.parse_playlists(soup('<details name="playlist-accordion"><summary>Solo Arena 0 Players</summary></details>'))
        data = dict(online=0, in_game=0, weekly=0, best='Unavailable', playlists=lists)
        self.assertIn('No active playlists', feed.payload(data, datetime.now(feed.UTC))['embeds'][0]['description'])

    def test_eastern_today_uses_next_utc_day(self):
        grid = [[0] * 24 for _ in range(7)]
        grid[1][1] = 100  # Tuesday 01:00 UTC = Monday 8 PM EST.
        now = datetime(2026, 11, 23, 14, tzinfo=feed.UTC)
        self.assertEqual(feed.best_today(grid, now, ZoneInfo('America/New_York')), '8 PM–9 PM EST (historical estimate)')

    def test_dst_and_local_date(self):
        grid = [[0] * 24 for _ in range(7)]
        grid[0][1] = 100
        # UTC Monday just after midnight is still local Sunday in July.
        now = datetime(2026, 7, 6, 0, tzinfo=feed.UTC)
        self.assertEqual(feed.best_today(grid, now, ZoneInfo('America/New_York')), '9 PM–10 PM EDT (historical estimate)')
        # Fall-back: second 1 AM hour is EST; local day contains 25 hours.
        grid = [[0] * 24 for _ in range(7)]
        grid[6][6] = 100
        now = datetime(2026, 11, 1, 12, tzinfo=feed.UTC)
        self.assertEqual(feed.best_today(grid, now, ZoneInfo('America/New_York')), '1 AM–2 AM EST (historical estimate)')

    def test_restart_edits_saved_message(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(feed, 'discord_request', return_value={'id': '12345'}) as request:
            path = Path(directory) / 'state.json'
            state = {}
            feed.update_board(None, 'unused', {}, state, path)
            self.assertEqual(request.call_args.args[1], 'POST')
            state = feed.read_state(path)
            feed.update_board(None, 'unused', {}, state, path)
            self.assertEqual(request.call_args.args[1:3], ('PATCH', 'unused/messages/12345'))

    def test_only_deleted_message_recreates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(feed, 'discord_request', side_effect=[feed.DiscordError(404, 10008), {'id': '456'}]) as request:
                feed.update_board(None, 'unused', {}, {'message_id': '123'}, path)
                self.assertEqual(request.call_count, 2)
            with patch.object(feed, 'discord_request', side_effect=feed.DiscordError(404, 10015)) as request:
                with self.assertRaises(feed.DiscordError):
                    feed.update_board(None, 'unused', {}, {'message_id': '123'}, path)
                self.assertEqual(request.call_count, 1)

    def test_rate_limit_respected(self):
        session = Mock()
        limited = Mock(status_code=429)
        limited.json.return_value = {'retry_after': 2.5}
        success = Mock(status_code=200)
        success.json.return_value = {'id': '123'}
        session.request.side_effect = [limited, success]
        with patch.object(feed.STOP, 'wait', return_value=False) as wait:
            self.assertEqual(feed.discord_request(session, 'PATCH', 'unused', {}), {'id': '123'})
            wait.assert_called_once_with(2.5)

    def test_corrupt_state_does_not_silently_post(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                feed.read_state(path)


if __name__ == '__main__':
    unittest.main()
