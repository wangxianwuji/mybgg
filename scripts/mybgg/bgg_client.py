import logging
import time
from xml.etree.ElementTree import fromstring

import declxml as xml
import requests
from requests_cache import CachedSession

logger = logging.getLogger(__name__)

class BGGClient:
    BASE_URL = "https://www.boardgamegeek.com/xmlapi2"

    def __init__(self, cache=None, debug=False):
        if not cache:
            self.requester = requests.Session()
        else:
            self.requester = cache.cache

        if debug:
            logging.basicConfig(level=logging.DEBUG)

    def collection(self, user_name, **kwargs):
        params = kwargs.copy()
        params["username"] = user_name
        data = self._make_request("/collection?version=1", params)
        collection = self._collection_to_games(data)
        return collection

    def plays(self, user_name):
        params = {
            "username": user_name,
            "page": 1,
        }
        all_plays = []

        data = self._make_request("/plays?version=1", params)
        new_plays = self._plays_to_games(data)

        while (len(new_plays) > 0):
            all_plays = all_plays + new_plays
            params["page"] += 1
            data = self._make_request("/plays?version=1", params)
            new_plays = self._plays_to_games(data)

        return all_plays

    def game_list(self, game_ids):
        if not game_ids:
            return []

        # Split game_ids into smaller chunks to avoid "414 URI too long"
        def chunks(iterable, n):
            for i in range(0, len(iterable), n):
                yield iterable[i:i + n]

        games = []
        for game_ids_subset in chunks(game_ids, 100):
            url = "/thing/?stats=1&id=" + ",".join([str(id_) for id_ in game_ids_subset])
            data = self._make_request(url)
            games += self._games_list_to_games(data)

        return games

    def _make_request(self, url, params={}, tries=0):

        try:
            response = self.requester.get(BGGClient.BASE_URL + url, params=params)
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
            if tries < 3:
                time.sleep(2)
                return self._make_request(url, params=params, tries=tries + 1)

            raise BGGException("BGG API closed the connection prematurely, please try again...")

        logger.debug("REQUEST: " + response.url)
        logger.debug("RESPONSE: \n" + prettify_if_xml(response.text))

        if response.status_code != 200:

            # Handle 202 Accepted
            if response.status_code == 202:
                if tries < 10:
                    time.sleep(5)
                    return self._make_request(url, params=params, tries=tries + 1)

            # Handle 504 Gateway Timeout
            if response.status_code == 540:
                if tries < 3:
                    time.sleep(2)
                    return self._make_request(url, params=params, tries=tries + 1)

            # Handle 429 Too Many Requests
            if response.status_code == 429:
                if tries < 3:
                    logger.debug("BGG returned \"Too Many Requests\", waiting 30 seconds before trying again...")
                    time.sleep(30)
                    return self._make_request(url, params=params, tries=tries + 1)

            raise BGGException(
                f"BGG returned status code {response.status_code} when requesting {response.url}"
            )

        tree = fromstring(response.text)
        if tree.tag == "errors":
            raise BGGException(
                f"BGG returned errors while requesting {response.url} - " +
                str([subnode.text for node in tree for subnode in node])
            )

        return response.text

    def _plays_to_games(self, data):
        def after_players_hook(_, status):
            return status["name"] if "name" in status else "Unknown"

        plays_processor = xml.dictionary("plays", [
            xml.array(
                xml.dictionary('play', [
                    xml.integer(".", attribute="id", alias="playid"),
                    xml.dictionary('item', [
                        xml.string(".", attribute="name", alias="gamename"),
                        xml.integer(".", attribute="objectid", alias="gameid")
                    ], alias='game'),
                    xml.array(
                        xml.dictionary('players/player', [
                            xml.string(".", attribute="name", required=False, default="Unknown")
                        ], required=False, alias='players', hooks=xml.Hooks(after_parse=after_players_hook))
                    )

                ], required=False, alias="plays")
            )
        ])

        plays = xml.parse_from_string(plays_processor, data)
        plays = plays["plays"]
        return plays

    def _collection_to_games(self, data):
        def after_status_hook(_, status):
            return [tag for tag, value in status.items() if value == "1"]

        game_in_collection_processor = xml.dictionary("items", [
            xml.array(
                xml.dictionary('item', [
                    xml.integer(".", attribute="objectid", alias="id"),
                    xml.integer(".", attribute="collid", alias="collection_id"),
                    xml.string("name"),
                    xml.string("thumbnail", required=False, alias="image"),
                    xml.string("version/item/thumbnail", required=False, alias="image_version"),
                    xml.string("version/item/name",  required=False, alias="version_name"),
                    xml.integer("version/yearpublished", attribute="value", alias="version_year", required=False),
                    xml.integer("version/item/link[@type='boardgamepublisher']", attribute="objectid", required=False, alias="publisher_id"),
                    xml.string("comment", required=False, alias="comment"),
                    xml.string("wishlistcomment", required=False, alias="wishlist_comment"),
                    xml.string("status", attribute="lastmodified", alias="last_modified"),
                    xml.dictionary("status", [
                        xml.string(".", attribute="fortrade"),
                        xml.string(".", attribute="own"),
                        xml.string(".", attribute="preordered"),
                        xml.string(".", attribute="prevowned"),
                        xml.string(".", attribute="want"),
                        xml.string(".", attribute="wanttobuy"),
                        xml.string(".", attribute="wanttoplay"),
                        xml.string(".", attribute="wishlist"),
                    ], alias='tags', hooks=xml.Hooks(after_parse=after_status_hook)),
                    xml.integer("numplays"),
                ], required=False, alias="items"),
            )
        ])
        collection = xml.parse_from_string(game_in_collection_processor, data)
        collection = collection["items"]
        return collection


    def _games_list_to_games(self, data):
        def numplayers_to_result(_, results):
            result = {result["value"].lower().replace(" ", "_"): int(result["numvotes"]) for result in results}

            if not result:
                result = {'best': 0, 'recommended': 0, 'not_recommended': 0}

            is_recommended = result['best'] + result['recommended'] > result['not_recommended']
            if not is_recommended:
                return "not_recommended"

            is_best = result['best'] > 10 and result['best'] > result['recommended']
            if is_best:
                return "best"

            return "recommended"

        def suggested_numplayers(_, numplayers):
            # Remove not_recommended player counts
            numplayers = [players for players in numplayers if players["result"] != "not_recommended"]

            # If there's only one player count, that's the best one
            if len(numplayers) == 1:
                numplayers[0]["result"] = "best"

            # Just return the numbers
            return [
                (players["numplayers"], players["result"])
                for players in numplayers
            ]

        def age_conversion(_, age_result):
            return int(age_result[:2])

        def suggested_playerage(_, playerages):

            suggested_ages = [ages for ages in playerages if ages["numvotes"] > 0]

            return suggested_ages

        def log_item(_, item):
            logger.debug("Successfully parsed: {} (id: {}).".format(item["name"], item["id"]))
            return item

        game_processor = xml.dictionary("items", [
            xml.array(
                xml.dictionary(
                    "item",
                    [
                        xml.integer(".", attribute="id"),
                        xml.string(".", attribute="type"),
                        xml.string("image", required=False),
                        xml.string("name[@type='primary']", attribute="value", alias="name"),
                        xml.array(
                            xml.string(
                                "name",
                                attribute="value",
                                required=False
                            ),
                            alias="alternate_names"
                        ),
                        xml.string("description"),
                        xml.array(
                            xml.string(
                                "link[@type='boardgamecategory']",
                                attribute="value",
                                required=False
                            ),
                            alias="categories",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgamefamily']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name")
                                ],
                                required=False
                            ),
                            alias="families",
                        ),
                        xml.array(
                            xml.string(
                                "link[@type='boardgamemechanic']",
                                attribute="value",
                                required=False
                            ),
                            alias="mechanics",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgameexpansion']", [
                                    xml.integer(".", attribute="id"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="expansions",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgamecompilation']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="contained",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgameimplementation']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="reimplements",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgameintegration']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="integrates",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgamedesigner']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="designers",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgameartist']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="artists",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgamepublisher']", [
                                    xml.integer(".", attribute="id"),
                                    xml.string(".", attribute="value", alias="name"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="publishers",
                        ),
                        xml.array(
                            xml.dictionary(
                                "link[@type='boardgameaccessory']", [
                                    xml.integer(".", attribute="id"),
                                    xml.boolean(".", attribute="inbound", required=False),
                                ],
                                required=False
                            ),
                            alias="accessories",
                        ),
                        xml.array(
                            xml.dictionary("poll[@name='suggested_numplayers']/results", [
                                xml.string(".", attribute="numplayers"),
                                xml.array(
                                    xml.dictionary("result", [
                                        xml.string(".", attribute="value"),
                                        xml.integer(".", attribute="numvotes"),
                                    ], required=False),
                                    hooks=xml.Hooks(after_parse=numplayers_to_result)
                                )
                            ],
                            required=False),
                            alias="suggested_numplayers",
                            hooks=xml.Hooks(after_parse=suggested_numplayers),
                        ),
                        xml.string(
                            "statistics/ratings/averageweight",
                            attribute="value",
                            alias="weight"
                        ),
                        xml.string(
                            "statistics/ratings/ranks/rank[@friendlyname='Board Game Rank']",
                            attribute="value",
                            required=False,
                            alias="rank"
                        ),
                        xml.array(
                            xml.dictionary("statistics/ratings/ranks/rank", [
                                xml.string(".", attribute="friendlyname"),
                                xml.string(".", attribute="value"),
                                xml.string(".", attribute="id"),
                            ],
                                required=False),
                            alias="other_ranks",
                        ),
                        xml.string(
                            "statistics/ratings/usersrated",
                            attribute="value",
                            alias="usersrated"
                        ),
                        xml.string(
                            "statistics/ratings/average",
                            attribute="value",
                            alias="average"
                        ),
                        xml.string(
                            "statistics/ratings/owned",
                            attribute="value",
                            alias="numowned"
                        ),
                        xml.string(
                            "statistics/ratings/bayesaverage",
                            attribute="value",
                            alias="rating"
                        ),
                        xml.string("playingtime", attribute="value", alias="playing_time", required=False),
                        xml.integer("yearpublished", attribute="value", alias="year"),
                        xml.integer(
                            "minage",
                            attribute="value",
                            alias="min_age",
                            required=False,
                        ),
                        xml.integer(
                            "minplayers",
                            attribute="value",
                            alias="min_players",
                            required=False,
                        ),
                        xml.integer(
                            "maxplayers",
                            attribute="value",
                            alias="max_players",
                            required=False,
                        ),
                        xml.array(
                            xml.dictionary("poll[@name='suggested_playerage']/results/result", [
                                        xml.string(".", attribute="value", alias="age", hooks=xml.Hooks(after_parse=age_conversion)),
                                        xml.integer(".", attribute="numvotes"),
                                    ], required=False),
                            alias="suggested_playerages",
                            hooks=xml.Hooks(after_parse=suggested_playerage),
                        ),
                    ],
                    required=False,
                    alias="items",
                    hooks=xml.Hooks(after_parse=log_item),
                )
            )
        ])
        games = xml.parse_from_string(game_processor, data)
        games = games["items"]
        return games

class CacheBackendSqlite:
    def __init__(self, path, ttl):
        self.cache = CachedSession(
            cache_name=path,
            backend="sqlite",
            expire_after=ttl,
        # extension="",
            fast_save=True,
            allowable_codes=(200,)
        )

class BGGException(Exception):
    pass

def prettify_if_xml(xml_string):
    import xml.dom.minidom
    import re
    xml_string = re.sub(r"\s+<", "<", re.sub(r">\s+", ">", re.sub(r"\s+", " ", xml_string)))
    if not xml_string.startswith("<?xml"):
        return xml_string

    parsed = xml.dom.minidom.parseString(xml_string)
    return parsed.toprettyxml()
