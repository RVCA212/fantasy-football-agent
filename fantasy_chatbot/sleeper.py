import requests_cache
from urllib.parse import urljoin
from typing import Union, Optional
from pathlib import Path


class SleeperClient:
    def __init__(self, cache_path: str = '../.cache'):

        # config
        self.cache_path = cache_path
        self.session = requests_cache.CachedSession(
            Path(cache_path) / 'api_cache', 
            backend='sqlite',
            expire_after=60 * 60 * 24,
        )

        # API URLs
        self.base_url = 'https://api.sleeper.app/v1/'
        self.stats_url = 'https://api.sleeper.com/'
        self.cdn_base_url = 'https://sleepercdn.com/'
        self.graphql_url = 'https://sleeper.com/graphql'

        # useful metadata
        self.nfl_state = self.get_nfl_state()

    def _get_json(self, path: str, base_url: Optional[str] = None) -> dict:
        url = urljoin(base_url or self.base_url, path)
        return self.session.get(url).json()

    def _get_content(self, path: str) -> bytes:
        url = urljoin(self.cdn_base_url, path)
        return self.session.get(url).content

    def _graphql(self, operation_name: str, query: str, variables: Optional[dict] = None) -> dict:
        return self.session.post(self.graphql_url, data={
            "operationName": operation_name,
            "variables": variables or {},
            "query": query,
        }).json()

    def _get_ranks(self, season: Optional[int] = None):
        return {
            p['player_id']: {
                'rank_ppr': p['stats']['rank_ppr'],
                'pos_rank_ppr': p['stats']['pos_rank_ppr']
            } for p in self._get_json(
                f'stats/nfl/{season or self.nfl_state["season"]}?season_type=regular&position[]=DEF&position[]=K&position[]=QB&position[]=RB&position[]=TE&position[]=WR&order_by=pts_ppr',
                base_url=self.stats_url
            )}

    def get_players(self, season: Optional[int] = None, limit: Optional[int] = 800) -> dict:
        """Get top N players by projected points - helps limit the universe to only the realistic players"""
        res = self._get_json(
            f'projections/nfl/{season or self.nfl_state["season"]}?season_type=regular&position[]=DEF&position[]=K&position[]=QB&position[]=RB&position[]=TE&position[]=WR&order_by=pts_ppr',
            base_url=self.stats_url
        )
        player_ranks = self._get_ranks(season or self.nfl_state['season'])
        if limit:
            return {p['player_id']: {**p['player'], **player_ranks[p['player_id']]} for p in res[:limit]}
        else:
            return {p['player_id']: {**p['player'], **player_ranks[p['player_id']]} for p in res}

    def get_player_stats(self, player_id: Union[str, int], season: Optional[int] = None, group_by_week: bool = False):
        return self._get_json(
            f'stats/nfl/player/{player_id}?season_type=regular&season={season or self.nfl_state["season"]}{"&grouping=week" if group_by_week else ""}',
            base_url=self.stats_url)

    def get_player(self, player_id: Union[str, int], season: Optional[int] = None):
        if player_stats := self.get_player_stats(player_id, season, group_by_week=False):
            return {
                **player_stats['player'],
                **player_stats['stats'],
            }
        return None

    def get_player_projections(self, player_id: Union[str, int], season: Optional[int] = None):
        return self._get_json(
            f'projections/nfl/player/{player_id}?season_type=regular&season={season or self.nfl_state["season"]}&grouping=week',
            base_url=self.stats_url)

    def get_player_news(self, player_id: Union[str, int], limit: int = 2) -> list[dict]:
        query = f"""query get_player_news_for_ids {{
            news: get_player_news(sport: "nfl", player_id: "{player_id}", limit: {limit}){{
                metadata
                player_id
                published
                source
                source_key
                sport
            }}
        }}"""
        return self._graphql(operation_name='get_player_news_for_ids', query=query)['data']['news']

    def get_league_drafts(self, league_id: str):
        return self._get_json(f'league/{league_id}/drafts')

    def get_draft_picks(self, draft_id: str):
        return self._get_json(f'draft/{draft_id}/picks')

    def get_league(self, league_id: str) -> dict:
        return self._get_json(f'league/{league_id}')

    def get_league_rosters(self, league_id: str) -> dict:
        return self._get_json(f'league/{league_id}/rosters')

    def get_league_matchups(self, league_id: str, week: Optional[int] = None) -> dict:
        week = week or self.nfl_state['display_week']
        return self._get_json(f'league/{league_id}/matchups/{week}')

    def get_league_standings(self, league_id: str):
        query = f"""query metadata {{
            metadata(type: "league_history", key: "{league_id}"){{
                key
                type
                data
                last_updated
                created
            }}    
        }}"""
        return sorted(self._graphql(operation_name='metadata', query=query)['data']['metadata']['data']['standings'],
                      key=lambda x: (x['wins'], x['fpts']), reverse=True)

    def get_league_users(self, league_id: str):
        return self._get_json(f'league/{league_id}/users')

    def get_transactions(self, league_id, week: Optional[int] = None):
        week = week or self.nfl_state['display_week']
        return self._get_json(f'league/{league_id}/transactions/{week}')

    def get_nfl_state(self):
        return self._get_json('state/nfl')

    def get_avatar(self, avatar_id: str, thumbnail: bool = True):
        return self._get_content(f'avatars/{"thumbs/" if thumbnail else ""}{avatar_id}')

    def get_user(self, user_id: str):
        """user_id can either be the id or username"""
        return self._get_json(f'user/{user_id}')

    def get_leagues_for_user(self, user_id: str, season: Optional[Union[str, int]] = None, sport: str = 'nfl'):
        season = season or self.nfl_state['season']
        return self._get_json(f'user/{user_id}/leagues/{sport}/{season}')

    def get_all_weekly_projections(self, season: Optional[Union[str, int]] = None,
                                   week: Optional[Union[str, int]] = None):
        season = season or self.nfl_state['season']
        week = week or self.nfl_state['display_week']
        return self._get_json(
            f'projections/nfl/{season}/{week}?season_type=regular&position[]=DEF&position[]=K&position[]=QB&position[]=RB&position[]=TE&position[]=WR&order_by=pts_ppr',
            base_url=self.stats_url
        )
