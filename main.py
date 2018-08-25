import time
import random
import re
from os.path import join

import cherrypy
import mako.lookup
import mysql.connector

import ps2ranks.libs.glicko2 as glicko2
import ps2ranks.libs.config as config

# relative to the web root, which should be above this directory
PROJECT_NAME = 'ps2ranks'
DATA_DIR = join(PROJECT_NAME, 'data/')
TEMPLATE_DIR = join(PROJECT_NAME, 'templates/')
MODULE_DIR = join(TEMPLATE_DIR, 'compile/')  # compiled templates

PAGE_TEMPLATE = 'ps2ranks.txt'

loader = mako.lookup.TemplateLookup(
    directories=[TEMPLATE_DIR],
    collection_size=25,
    module_directory=MODULE_DIR,
    output_encoding='utf-8',
    input_encoding='utf-8',
    strict_undefined=True)


glicko_ops = glicko2.Glicko2(tau=0.3, sigma=0.2)


SUBTITLE_COOKIE = ['360 noscope', 'tryhard',
    "ADADADAD", "1hit", 'face rocket', 'battle galaxy',
    "lolpods","lasher","get liberated", 'gg uninstall',
    'hover pilot', 'noobtube',
    'shotgun', 'spawncamp', 'spawnshield', 'nanite systems', 'scope sway',
    'screenshake', 'lettuce', 'bruce lee', 'hatemail', 'gaming condoms',
    'edukat mby?', 'fukitol', 'get rekt', 'beta male', 'dunked on',
    'lagjump',
    'stealth nerf', '4th faction', 'skillhawk', 'lockon',
    'mlg midair lib switch',
    'streamsnipe', 'teamkill', 'fake stats', 'mlg midair knife kill',
    'emorage', 'gamerfood','8 fps','soe pls',
    '"buy a better computer noob"',
    'kill someone so he respawns at your sundy for 2xp',
    'celebrate 1 year ps2 by going beta again',
    'lockons: the game: special',
    'Stardouserx: redeploy the devs to eqn',
    'Stardouserx: orion extreme spray weapon',
    'Avakael: I am surrounded by fucking casuals',
    'amerish crash', 'indar crash', 'esamir crash', 'hossin crash',
    'login server down','15 months of beta',

    'nerf TR','biofarm','new helmet','ghostcap',
    'empty amerish']

def get_subtitle():
    return random.choice(SUBTITLE_COOKIE)

"""
the mysql story is the following (points toward connection per request):
* old mysql.connector versions have no pooling, so creating a connection per
request is a waste, because response time increases. (-1 pt)
* connection establishment won't be that bad, because mysql is usually on
localhost and not encrypted (+1 pt)
* at the same time, the site isn't very popular, and it might not be worth it
to keep a connection open all the time. (+1 pt)
* also when more projects are added and if each gets a persistent connection,
we just get idle connections, again a waste. (+1 pt)
* a connection per request (each being in its own thread) means we have to keep
track of how many threads exist and tune the db connection limits accordingly.
(-1 pt)
** it's easy to set limis with uwsgi on how many threads and processes
exist. (+1 pt)
* i don't want to roll my own connection pool, because a library upgrade
someday gets it for free (+1 pt)
* error handling is complex and messy either way (+0 pt)
"""

class MysqlOperations:
    def __init__(self, user, passwd, host, db):
        self.user, self.passwd, self.host, self.db = user, passwd, host, db
        self.ctx = None # connection.MySQLConnection
        self.cursor = None # cursor.MySQLCursor

    def connect(self):
        try:
            self.ctx = mysql.connector.connect(
                user=self.user,
                password=self.passwd,
                host=self.host,
                database=self.db,
                raise_on_warnings=True)
            self.cursor = self.ctx.cursor()
            return self.ctx, self.cursor
        except mysql.connector.Error as err:
            self.cleanup()
            raise

    def cleanup(self):
        # hope everything was fetched from cursor or it can throw
        # i rather see it happen and fix it, than work around here
        if self.cursor is not None:
            self.cursor.close()
            self.cursor = None
        if self.ctx is not None:
            self.ctx.close()
            self.ctx = None



"""
all form fields take one or more comma separated alphanumeric strings,
so the same function can parse input.
"""
def parse_query_arg(qs_dict, arg):
    strings = []
    if arg not in qs_dict:
        return strings
    query = qs_dict[arg]
    query = re.sub('\s', '', query) # remove spaces
    split = query.split(',')
    filtered = filter(lambda x: x != '', split)
    for string in filtered:
        if re.match('^\w+$', string, re.ASCII):
            strings.append(string)
    return strings

# use a case insensitive collation

GLOBAL_TOP1000 = """
    select name, rating, rd, br, faction, world
    from chartbl
    force index (rating)
    order by rating DESC
    limit 0,1000"""

WORLD_TOP1000 = """
    select name, rating, rd, br, faction, world
    from chartbl where world=%s
    order by rating DESC
    limit 0,1000"""

NUM_PLAYERS = """
    select count(name)
    from chartbl"""

AVG_RATING = """
    select value
    from summarytbl
    where variable='avg_rating'"""

LAST_DB_UPDATE = """
    select value
    from summarytbl
    where variable='last_update'"""

# this gets extended with OR name LIKE %s OR name LIKE %s ...
PREFIX_SEARCH = """
    select name, rating, rd
    from chartbl
    where name like %s {}
    order by rating DESC
    limit 0, 100"""

PLAYER_WORLD_RATING = """
    select world, rating
    from chartbl
    where name = %s"""

PLAYER_GLOBAL_RANK = """
    select count(*)
    from chartbl
    where rating >= %s"""

PLAYER_SERVER_RANK = """
    select count(*)
    from chartbl
    where world=%s and rating >=%s"""

WINPCT_RATINGS = """
     select name, rating, rd
     from chartbl
     where name in (%s, %s)
     """

def get_top1000(qs_dict, cursor):
    world = 'global'
    strings = parse_query_arg(qs_dict, 'top')
    if len(strings):
        world = strings[0]

    if world.lower() == 'global':
        cursor.execute(GLOBAL_TOP1000)
    else:
        cursor.execute(WORLD_TOP1000, (world, ))
    top_rows = cursor.fetchall()
    ranklist = []
    i = 1
    for name, rating, rd, br, faction, world in top_rows:
        ranklist.append(
            (i, name, round(rating), round(rd), br, faction, world))
        i += 1
    return ranklist

def get_avg_rating(cursor):
    cursor.execute(AVG_RATING)
    avg_rating = cursor.fetchone()
    if avg_rating is None:
        return '???'
    else:
        return avg_rating[0]

def get_num_players(cursor):
    cursor.execute(NUM_PLAYERS)
    return cursor.fetchone()[0]

def get_last_db_update(cursor):
    cursor.execute(LAST_DB_UPDATE)
    last_update = cursor.fetchone()
    if last_update is None:
        return '???'
    else:
        return int(time.time()) - int(last_update[0])

def gen_search_query(searches):
    """
    return a finished parametrized query
    also return searches with a % appended for prefix search
    """
    searches = [search + '%' for search in searches]

    where_clause = ''
    for idx in range(1, len(searches)):
        where_clause += ' OR name LIKE %s '
    query = PREFIX_SEARCH.format(where_clause)
    return query, searches

def cook_search_results(uncooked):
    results = []
    i = 1
    for name, rating, rd in uncooked:
        results.append((i, name, round(rating), round(rd)))
        i += 1
    return results

def get_search_ratings(qs_dict, cursor):
    SEARCH_MAX = 10
    ratings = []
    searches = parse_query_arg(qs_dict, 'search')
    searches = searches[:SEARCH_MAX]

    if len(searches):
        query, searches_suffixed = gen_search_query(searches)
        cursor.execute(query, searches)
        uncooked = cursor.fetchall()

        ratings = cook_search_results(uncooked)
    return searches, ratings


def get_player_info(player, cursor):
    world = rating = None
    cursor.execute(PLAYER_WORLD_RATING, (player, ))
    results = cursor.fetchone()
    if results is not None:
        world, rating = results
    return world, rating

def get_global_rank(rating, cursor):
    cursor.execute(PLAYER_GLOBAL_RANK, (rating, ))
    return cursor.fetchone()[0]

def get_server_rank(world, rating, cursor):
    cursor.execute(PLAYER_SERVER_RANK, (world, rating))
    return cursor.fetchone()[0]

def format_rank_results(player, world, global_rank, global_pct, server_rank):
    if player is None:
        return ''
    if world is None:
        return 'No such nick is ranked. It might be deleted for inactivity.'
    global_pct = str(global_pct)[:6]  # eg 36.123
    return ('Ranked at ' + str(global_rank) +
            ' which is better than ' +
            global_pct + "% of ALL players. "
            "Ranked " + str(server_rank) + " on " + world + ".")

def get_rank_results(qs_dict, num_players, cursor):
    """
    * no getrank in query, should not display anything
    * empty getrank submitted, should not display anything
    * player not in db, display not found error
    * player found in db
    ** find server and global rankings
    ** display player name and rankings
    """
    players = parse_query_arg(qs_dict, 'getrank')
    player = world = global_rank = global_pct = server_rank = None
    if len(players):
        player = players[0]
        world, rating = get_player_info(player, cursor)
        if world is not None:
            global_rank = get_global_rank(rating, cursor)
            global_pct = 100 - round(global_rank/num_players*100, 3)
            server_rank = get_server_rank(world, rating, cursor)
    results = format_rank_results(player, world, global_rank,
        global_pct, server_rank)
    return player, results

def get_winpct(qs_dict, cursor):
    ERR_NEED_PLAYERS = ('Need exactly two players from the database. '
        'Maybe someone went inactive?')
    players = parse_query_arg(qs_dict, 'winpct')
    if len(players) != 2:
        results = ERR_NEED_PLAYERS
    else:
        cursor.execute(WINPCT_RATINGS, players)
        player_info = cursor.fetchall()
        if player_info is None or len(player_info) != 2:
            results = ERR_NEED_PLAYERS
        else:
            # create glicko, run 1v1, return results
            p1_tup, p2_tup = player_info
            p1 = glicko_ops.create_rating(p1_tup[1], p1_tup[2])
            p2 = glicko_ops.create_rating(p2_tup[1], p2_tup[2])
            p1_win, p2_win = glicko_ops.winpct_1vs1(p1, p2)
            result_str = ("{} has {}% chance to win against {}"
                    " and {} has {}% chance to win against {}.")
            results = result_str.format(
                p1_tup[0], round(p1_win, 3),
                p2_tup[0], p2_tup[0], round(p2_win, 3), p1_tup[0])
    return players, results


class Ps2ranks:
    def __init__(self, logger):
        self._logger = logger
        self._mysql_ops = MysqlOperations(
            config.mysql_user, config.mysql_pass, config.mysql_host,
            config.mysql_db)


    @cherrypy.expose
    def index(self, *args, **kwargs):
        try:
            ctx, cursor = self._mysql_ops.connect()
            avg_rating = get_avg_rating(cursor)
            num_players = get_num_players(cursor)
            ranklist = get_top1000(kwargs, cursor)
            last_db_update = get_last_db_update(cursor)
            searches, ratings = get_search_ratings(kwargs, cursor)
            rankplayer, rank_results = get_rank_results(
                kwargs, num_players, cursor)
            winpct_players, winpct_results = get_winpct(kwargs, cursor)
            ctx.commit()
            subtitle = get_subtitle()
            template = loader.get_template(PAGE_TEMPLATE)
            return template.render(
                subtitle=subtitle,
                ranklist=ranklist,
                num_players=num_players,
                avg_rating=avg_rating,
                last_db_update=last_db_update,
                search_args=', '.join(searches),
                ratings=ratings,
                rankplayer=rankplayer,
                rank_results=rank_results,
                winpct_players=', '.join(winpct_players),
                winpct_results=winpct_results)
        except mysql.connector.Error as err:
            self._logger.exception(err)
            return 'An error occurred. Please try again later.'
        finally:
            self._mysql_ops.cleanup()
    index.exposed = True
