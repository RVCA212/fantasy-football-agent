from typing import Annotated, TypedDict, Literal, Optional
from rapidfuzz import process, fuzz
import pandas as pd
from sleeper import SleeperClient
import config as cf


class Lineup(TypedDict):
    starters: list[dict]
    bench: list[dict]


class League:
    def __init__(self, league_id: str, client: SleeperClient = SleeperClient(), week: Optional[int] = None):

        self.client = client
        self.league_id = league_id
        self.week = week or client.nfl_state['display_week']
        self.league = client.get_league(league_id)
        self.player_data = client.get_players(limit=None)

        # users in the league
        self.league_users = client.get_league_users(league_id)
        self.username_to_user_id = {u['display_name']: u['user_id'] for u in self.league_users}
        self.user_id_to_user = {u['user_id']: u for u in self.league_users}

        # rosters
        self.rosters = client.get_league_rosters(league_id)
        self.roster_id_to_user_id = {r['roster_id']: r['owner_id'] for r in self.rosters}
        self.user_id_to_roster_id = {v: k for k, v in self.roster_id_to_user_id.items()}

        # matchups are more useful for getting starters by week
        self.matchups = client.get_league_matchups(league_id, week=week)
        self.user_id_to_roster = {self.roster_id_to_user_id[m['roster_id']]: m for m in self.matchups}

        # player to owner
        self.player_id_to_owner = {}
        for user_id, roster in self.user_id_to_roster.items():
            for player_id in roster['players']:
                self.player_id_to_owner[player_id] = self.user_id_to_user[user_id]['display_name']

        # player names/ids
        self.player_name_to_id: dict[str, str] = {f"{v['first_name']} {v['last_name']}": k for k, v in self.player_data.items()}
        self.player_names: list[str] = list(self.player_name_to_id.keys())

        # draft
        self.player_id_to_draft_position = {}
        latest_draft_id = sorted(self.client.get_league_drafts(league_id), key=lambda x: x['start_time'], reverse=True)[0]['draft_id']
        for pick in self.client.get_draft_picks(latest_draft_id):
            self.player_id_to_draft_position[pick['player_id']] = f"Round {pick['round']} Pick {pick['pick_no']}"

        # waivers - top 10 available at each position by projected points this week
        self.top_available_by_position = {position: [] for position in cf.POSITIONS}
        self.weekly_projections = self.client.get_all_weekly_projections(week=week)  # already sorted by projected points
        for player_proj in self.weekly_projections:
            # need to check if a player is already on a roster
            if self.player_id_to_owner.get(player_proj['player_id']):
                continue
            position = player_proj['player']['position']
            if (position not in cf.POSITIONS) or (len(self.top_available_by_position[position]) >= 10):
                continue

            self.top_available_by_position[position].append({
                'name': f"{player_proj['player']['first_name']} {player_proj['player']['last_name']}",
                'position': position,
                'team': player_proj['player']['team'],
                'opponent': player_proj['opponent'],
                'projected_points': player_proj['stats']['pts_ppr']
            })

    @classmethod
    def from_user_default_league(cls, username: str):

        client = SleeperClient()
        # have to get the user_id
        user = client.get_user(username)

        # default league - if user is in more than one league, you should have them select
        default_league = client.get_leagues_for_user(user['user_id'])[0]
        return cls(league_id=default_league['league_id'])

    def get_lineup_for_owner(self, username: str) -> Lineup:
        roster = self.user_id_to_roster[self.username_to_user_id[username]]
        return {
            'starters': [self.player_data[player_id] for player_id in roster['starters']],
            'bench': [self.player_data[player_id] for player_id in
                      set(roster['players']).difference(roster['starters'])]
        }

    def get_player_id_fuzzy_search(self, player_name: str) -> tuple[str, str]:
        # will need a simple search engine to go from player name to player id without needing exact matches. returns the player_id and matched player name as a tuple
        nearest_name = process.extract(query=player_name, choices=self.player_names, scorer=fuzz.WRatio, limit=1)[0]
        return self.player_name_to_id[nearest_name[0]], self.player_names[nearest_name[2]]

    def get_player_current_owner(self, player_name: str) -> str:
        """Get a player's current owner. If they are not on a team, they are a free agent"""
        player_id, player_name = self.get_player_id_fuzzy_search(player_name)

        return f'Current owner of {player_name} is ' + self.player_id_to_owner.get(player_id, 'Free Agent')

    def get_league_standings_df(self) -> pd.DataFrame:
        standings = self.client.get_league_standings(self.league_id)
        standings_list = [{
            'rank': idx + 1,
            'team_owner': self.user_id_to_user[self.roster_id_to_user_id[s['roster_id']]]['display_name'],
            'team_name': self.user_id_to_user[self.roster_id_to_user_id[s['roster_id']]]['metadata'].get(
                'team_name', f"Team {self.user_id_to_user[self.roster_id_to_user_id[s['roster_id']]]['display_name']}"
            ),
            'record': f"{s['wins']}-{s['losses']}",
            'points_for': s['fpts'],
            'points_against': s['fpts_against'],
            'num_transactions': s['total_transactions'],
        } for idx, s in enumerate(standings)]

        return pd.DataFrame(standings_list)

    def get_league_status(self) -> str:
        """Retrieve overall league status, e.g. standings, current week, playoff details"""

        standings_df = self.get_league_standings_df()

        league_settings = self.league['settings']
        num_playoff_teams = league_settings['playoff_teams']
        playoffs_start_week = league_settings['playoff_week_start']
        current_week = self.client.nfl_state['week']

        league_status = f"""League Name: {self.league['name']}
Current NFL Week: {current_week}
Fantasy Playoffs Start Week: {playoffs_start_week}
Number of Playoff Teams: {num_playoff_teams} (out of {len(standings_df)})
Standings:
{standings_df.to_markdown(index=False)}"""
        return league_status

    def get_player_stats_df(self, player_name: Annotated[str, "The player's name."]) -> pd.DataFrame:
        player_id, player_name = self.get_player_id_fuzzy_search(player_name)
        # stats
        player_stats = self.client.get_player_stats(player_id, group_by_week=True)
        weekly_stats = []
        for week in range(1, self.client.nfl_state['display_week']):
            stats_for_week = player_stats[str(week)] or {'opponent': None, 'stats': {'pts_ppr': 0}}
            weekly_stats.append({
                'week': week, 
                'opponent': stats_for_week['opponent'],
                'points': stats_for_week['stats'].get('pts_ppr', 0)
            })
        return pd.DataFrame(weekly_stats)

    def get_player_stats(self, player_name: Annotated[str, "The player's name."]) -> str:
        """Get this year's stats (points per week and opponents) for a player from their name. Returned as a table."""
        stats_df = self.get_player_stats_df(player_name)
        return f"{self.client.nfl_state['season']} Stats for {player_name}\n" + stats_df.to_markdown(index=False)

    def get_player_news(self, player_name: Annotated[str, "The player's name."]) -> str:
        """
        Get recent news about a player for the most up-to-date analysis and injury status.
        Use this whenever naming a player in a potential deal, as you should always have the right context for a recommendation.
        If sources are provided, include markdown-based link(s)
        (e.g. [Rotoballer](https://www.rotoballer.com/player-news/saquon-barkley-has-historic-night-sunday/1502955) )
        at the bottom of your response to provide proper attribution
        and allow the user to learn more.
        """
        player_id, player_name = self.get_player_id_fuzzy_search(player_name)
        # news
        news = self.client.get_player_news(player_id, limit=3)
        player_news = f"Recent News about {player_name}\n\n"
        for n in news:
            player_news += f"**{n['metadata']['title']}**\n{n['metadata']['description']}"
            if analysis := n['metadata'].get('analysis'):
                player_news += f"\n\nAnalysis:\n{analysis}"
            if url := n['metadata'].get('url'):
                # markdown link to source
                player_news += f"\n[{n['source'].capitalize()}]({url})\n\n"

        return player_news

    def get_player_draft_position(self, player_name: Annotated[str, "The player's name."]) -> str:
        """
        Get a player's original draft position (round and pick). Useful for assessing trades/potential keepers.
        Returns `Undrafted` if a player was not drafted
        """
        player_id, player_name = self.get_player_id_fuzzy_search(player_name)
        return self.player_id_to_draft_position.get(player_id, 'Undrafted')
    
    def get_player_rankings_df(self, position: Optional[Literal['QB', 'RB', 'WR', 'TE', 'K', 'DEF']] = None) -> pd.DataFrame:
        
        players_at_position = filter(lambda x: x['position'] == position, self.player_data.values()) if position else self.player_data.values()
        sort_key = 'pos_rank_ppr' if position else 'rank_ppr'

        player_rankings: list[dict] = []
        for player in sorted(players_at_position, key=lambda x: x[sort_key])[:30]:
            player_name = f"{player['first_name']} {player['last_name']}"
            player_rankings.append({
                'name': player_name,
                'position': player['position'],
                'team': player['team'],
                'pos_rank_ppr': player['pos_rank_ppr'],
                'rank_ppr': player['rank_ppr'],
                'injury_status': player['injury_status'],
                'draft_position': self.get_player_draft_position(player_name),
            })
        return pd.DataFrame(player_rankings).sort_values(sort_key)
    
    def get_player_rankings(self, position: Optional[Literal['QB', 'RB', 'WR', 'TE', 'K', 'DEF']] = None) -> str:
        """Get scoring rankings for the season so far. Can be broken down by position by providing an optional `position` arg. 
        If `position` is unspecified or null, overall rankings will be returned."""

        return f'Rankings so far for position {position or "overall"}\n\n' + self.get_player_rankings_df(position).to_markdown(index=False)

    def get_roster_for_team_owner_df(self, owner: Annotated[str, "The username of the team owner."]) -> Optional[pd.DataFrame]:

        if owner not in self.username_to_user_id:
            return None
        roster = self.user_id_to_roster[self.username_to_user_id[owner]]

        roster_bench = [p for p in set(roster['players']).difference(roster['starters'])]

        roster_details = []

        for player_id in roster['starters'] + roster_bench:
            try:
                player = self.player_data[player_id]
            except KeyError:
                player = self.client.get_player(player_id)

            # projections
            projections = self.client.get_player_projections(player_id)[str(self.client.nfl_state['display_week'])]

            if player:
                player_name = f"{player['first_name']} {player['last_name']}"
                roster_details.append({
                    'name': player_name,
                    'position': player['position'],
                    'team': player['team'],
                    'position_rank': player['pos_rank_ppr'],
                    'overall_rank': player['rank_ppr'],
                    'is_current_starter': player_id in roster['starters'],
                    'projected_points': projections['stats']['pts_ppr'] if projections else None,
                    'opponent': projections['opponent'] if projections else None,
                    'injury_status': player['injury_status'],
                    'draft_position': self.get_player_draft_position(player_name),
                })
        return pd.DataFrame(roster_details)

    def get_roster_for_team_owner(self, owner: Annotated[str, "The username of the team owner."]) -> str:
        """Retrieve roster details for a team based on the owner's username"""
        roster_df = self.get_roster_for_team_owner_df(owner)
        if roster_df is not None:
            return f'Roster for {owner}:\n\n' + roster_df.to_markdown(index=False)
        else:
            return f'Owner {owner} not found. Available owners: {list(self.username_to_user_id.keys())}'

    def get_best_available_at_position_df(self, position: Literal['QB', 'RB', 'WR', 'TE', 'K', 'DEF']) -> pd.DataFrame:
        return pd.DataFrame(self.top_available_by_position[position])

    def get_best_available_at_position(self, position: Literal['QB', 'RB', 'WR', 'TE', 'K', 'DEF']):
        """Get the top 10 best available players not currently rostered (waiver wire) at a given position based on projected points for the current week"""
        return self.get_best_available_at_position_df(position).to_markdown(index=False)
