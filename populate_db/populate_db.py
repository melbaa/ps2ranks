import time
import urllib.request
import socket
import logging
import datetime

import mysql.connector
import tornado.ioloop
import requests

import libs.glicko2 as glicko2


def mysql_connect(user, password, host, database):
    ctx = mysql.connector.connect(
        user=user,
        password=password,
        host=host,
        database=database,
        raise_on_warnings=True)
    cursor = ctx.cursor()
    return ctx, cursor


def determine_sleep_time(num_events, time_to_sleep, TIME_TO_SLEEP_DEFAULT):
    """
       num_events is None if soe api returned an error or servers down
        sleep a little
    """
    min_sleep = 5  # soe starts throttling at some point, where?

    if num_events is None:
        return TIME_TO_SLEEP_DEFAULT
    elif num_events > 800:
        time_to_sleep -= 3
    elif num_events == 0:  # servers down?
        pass  # keep the same sleep
    elif num_events < 650:
        time_to_sleep += 1

    if time_to_sleep < min_sleep:
        time_to_sleep = min_sleep
    return time_to_sleep


def setup_logging():
    logger = logging.getLogger(__name__)
    logger.propagate = False  # or we get a duplicate msg in root logger
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    logger.setLevel(logging.DEBUG)

    # add ch to logger
    logger.addHandler(ch)

    return logger


"""
  don't keep a ref to the ctx or cursor, because they might change
  use the property instead
"""


class SqlConnectionCtx:

    def __init__(self, user, password, host, database, logger):
        self.user = user
        self.password, self.host, self.database = password, host, database
        self._ctx, self._cursor = mysql_connect(user, password, host, database)
        self.logger = logger

    @property
    def cursor(self):
        return self._cursor

    def commit(self):
        try:
            self._ctx.commit()
        except mysql.connector.Error as err:
            log_msg = "something went wrong: {}".format(str(err))
            self.logger.error(log_msg)

    def close(self):
        self._cursor.close()
        self._ctx.close()

    def reconnect(self):
        self.logger.debug('trying to reconnect')
        self.close()
        self._ctx, self._cursor = mysql_connect(
            self.user,
            self.password,
            self.host,
            self.database)
        self.logger.debug('reconnected successfully')


class DeleteInactive:

    def __init__(self, ioloop, logger, conn_ctx):
        self.ioloop = ioloop
        self.logger = logger
        self.conn_ctx = conn_ctx

        # magical dynamic queries full of repetition
        self.max_lastseen_qry = """
select max(lastseen)
from chartbl
"""
        self.old_qry_predicate = """
where ({}-lastseen)/3600/24 > (br*0.2+11)
"""
        self.count_old_qry = """
select count(*)
from chartbl
{}
""".format(self.old_qry_predicate)

        self.del_old_qry = """
delete from chartbl
{}
limit 1000
""".format(self.old_qry_predicate)

        self.select_old_qry = """
select name,
       br,
       lastseen/3600/24,
       ({}-lastseen)/3600/24,
       '>',
       br*0.2+11
from chartbl
{}
limit 20
""".format("{}", self.old_qry_predicate)

    def delete_tick(self):
        try:

            self.conn_ctx.cursor.execute(self.max_lastseen_qry)
            max_lastseen = self.conn_ctx.cursor.fetchone()[0]

            del_stmt = self.del_old_qry.format(max_lastseen)

            self.conn_ctx.cursor.execute(
                self.count_old_qry.format(max_lastseen))
            cnt_players = self.conn_ctx.cursor.fetchone()

            log_msg = "{} players pending delete with the statement {}".format(
                cnt_players,
                del_stmt)
            self.logger.debug(log_msg)

            self.conn_ctx.cursor.execute(self.select_old_qry.format(
                max_lastseen,
                max_lastseen))
            rows = self.conn_ctx.cursor.fetchall()
            for r in rows:
                self.logger.debug(str(r))

            self.conn_ctx.cursor.execute(del_stmt)

            log_msg = "deleted {} rows gg".format(
                self.conn_ctx.cursor.rowcount)
            self.logger.debug(log_msg)

        except mysql.connector.Error as err:
            log_msg = "something went wrong: {}".format(str(err))
            self.logger.error(log_msg)
        except Exception as e:
            self.logger.error("what's going on? " + str(e), exc_info=True)

        self.conn_ctx.commit()

        cb = self.delete_tick
        self.ioloop.add_timeout(datetime.timedelta(minutes=30), cb)


class SummaryTableUpdate:

    def __init__(self, ioloop, logger, conn_ctx):
        self.ioloop = ioloop
        self.logger = logger
        self.conn_ctx = conn_ctx

        self.insert_update_qry = """
insert into summarytbl (variable, value)
values ('avg_rating', %s)
on duplicate key update value=%s
"""

        self.find_avg_rating_qry = """
select avg(rating)
from chartbl
"""

    def avg_player_rating_tick(self):

        try:
            self.conn_ctx.cursor.execute(self.find_avg_rating_qry)
            avg = int(self.conn_ctx.cursor.fetchone()[0])

            self.conn_ctx.cursor.execute(
                self.insert_update_qry, (str(avg), str(avg)))
            log_msg = 'in summarytbl, avg_rating={}'.format(avg)
            self.logger.debug(log_msg)
        except mysql.connector.Error as err:
            log_msg = "something went wrong: {}".format(str(err))
            self.logger.error(log_msg)

        self.conn_ctx.commit()
        cb = self.avg_player_rating_tick
        self.ioloop.add_timeout(datetime.timedelta(days=1), cb)


class Populate_db:

    def __init__(self, ioloop, serviceid, useragent, logger, conn_ctx):

        self.requests_session = requests.Session()
        self.soe_api_url = 'http://census.soe.com/' + \
            serviceid + '/get/ps2:v2/event'
        self.soe_api_params = {'type': 'KILL',
                               'c:limit': '1000',
                               'c:sort': 'timestamp:1',
                               'c:join': "character^on:attacker_character_id^to:character_id^inject_at:attacker^hide:times'name.first_lower'battle_rank.percent_to_next'certs'character_id'daily_ribbon'head_id'profile_id(faction^inject_at:faction^show:code_tag),character^on:character_id^to:character_id^inject_at:character^hide:times'name.first_lower'battle_rank.percent_to_next'certs'character_id'daily_ribbon'head_id'profile_id(faction^inject_at:faction^show:code_tag),world^on:world_id^inject_at:world^list:0^hide:state'world_id",  # NOQA
                               'after': 0
                               }
        self.soe_api_headers = {'Connection': 'Keep-Alive',
                                'User-Agent': useragent}

        self.ioloop = ioloop

        self.logger = logger
        self.logger.info('started listening to the soe api')

        """
        default_volatility (sigma) 0.06
        and tau (r constraint on volatility) 0.5 RD ~ 65
        sigma 0.5 and tau 1.2 RD ~ 200
        sigma 0.5 and tau 0.3 RD ~ 180
        sigma 0.02 and tau 0.3 RD ~ 30
        """
        self.glicko = glicko2.Glicko2(tau=0.3)
        self.default_rd = 100  # 350 was too much, spikes to n1
        self.default_rating = 1500
        self.default_volatility = 0.02  # sigma
        self.min_rd = 30.

        self.timestamp = 0

        self.TIME_TO_SLEEP_DEFAULT = 10
        self.time_to_sleep = self.TIME_TO_SLEEP_DEFAULT  # sec

        self.insert_qry = """
insert into chartbl
            (rating,
            rd,
            volatility,
            lastseen,
            lastopponent,
            name,
            world,
            faction,
            br)
values ( %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
        self.select_qry = """
select rating,
       rd,
       volatility
from chartbl
where name =%s
"""
        self.update_qry = """
update chartbl
set rating=%s,
    rd=%s,
    volatility=%s,
    lastseen=%s,
    world=%s,
    faction=%s,
    br=%s
where name=%s
"""

        self.last_update_qry = """
insert into summarytbl (variable,value)
values ('last_update', %s)
on duplicate key update value=%s
"""
        self.conn_ctx = conn_ctx
        self.db_error = False

    def select_update_tick(self):

        try:

            if self.db_error:

                self.conn_ctx.reconnect()
                self.db_error = False

            json_reply = self.request_soe_killboard()
            self.timestamp, num_events = self.update_glicko_ratings(
                json_reply, self.timestamp)
            frags_per_sec = round(num_events / self.time_to_sleep)
            self.time_to_sleep = determine_sleep_time(
                num_events,
                self.time_to_sleep,
                self.TIME_TO_SLEEP_DEFAULT)

            curr_time = int(time.time())
            self.conn_ctx.cursor.execute(
                self.last_update_qry, (curr_time, curr_time))

            msg = 'saw {} events, sleeping for {} sec, {} frags per sec'.format(
                num_events,
                self.time_to_sleep,
                frags_per_sec)
            self.logger.debug(msg)
        except urllib.error.URLError as e:  # eg no internets
            self.logger.error('wtf urlerror' + str(e))
        except socket.error as e:  # eg no internet or after hibernate
            self.logger.error('wtf socket.error: ' + str(e))
        except mysql.connector.errors.InterfaceError as e:
            # broken connection to mysql
            self.logger.error('wtf InterfaceError error: ' + str(e))
        except mysql.connector.errors.DatabaseError as e:
            self.logger.error('wtf DatabaseError error: ' + str(e))
            self.db_error = True
        except mysql.connector.errors.OperationalError as e:
            self.logger.error('wtf operational error: ' + str(e))
        except Exception as e:
            self.logger.error("what's going on? " + str(e), exc_info=True)

        cb = self.select_update_tick
        self.ioloop.add_timeout(
            datetime.timedelta(seconds=self.time_to_sleep), cb)

    def find_nick_or_default(self, nick):

        self.conn_ctx.cursor.execute(self.select_qry, (nick,))
        # print(self.conn_ctx.cursor.statement)

        result = self.conn_ctx.cursor.fetchone()

        # self.logger.debug('old stats for {}: {}'.format(nick, result))
        rating, rd = 0, 0
        if result is None:
            # insert defaults
            try:
                self.conn_ctx.cursor.execute(
                    self.insert_qry,
                    (self.default_rating,
                        self.default_rd,
                        self.default_volatility,
                        0,
                        '',
                        nick,
                        '',
                        '',
                        0))
                # self.logger.debug('adding new player with qry ' + self.conn_ctx.cursor.statement)
            except mysql.connector.errors.IntegrityError as e:
                self.logger.error(e, 'wtf integrity Error because of insert')
            rating, rd, sigma = float(self.default_rating), float(
                self.default_rd), float(self.default_volatility)
        else:
            rating, rd, sigma = float(result[0]), float(
                result[1]), float(result[2])

        # we want to keep rd high enough, so changes happen fast enough
        if rd < self.min_rd:
            rd = self.min_rd
        return rating, rd, sigma

    def request_soe_killboard(self):
        # request killboard from soe
        # timestamp is 0 the first time, updated by the data we receive later

        # update params to reflect new timestamp
        self.soe_api_params.update(after=self.timestamp)

        self.logger.debug('making request to {} with params {}'.format(
            self.soe_api_url,
            self.soe_api_params))

        reply = self.requests_session.get(self.soe_api_url,
                                          timeout=60,
                                          params=self.soe_api_params,
                                          headers=self.soe_api_headers
                                          )

        # self.logger.debug(reply.headers)
        # self.logger.debug(reply.request.headers)
        # self.logger.debug(reply.url)
        # self.logger.debug(reply.content)

        json_reply = reply.json()

        return json_reply

    def update_glicko_ratings(self, json_reply, old_timestamp):
        num_events = 0
        timestamp = old_timestamp
        try:
            if 'errorCode' in json_reply:
                self.logger.error(
                    'soe returned error {}'.format(str(json_reply)))

            elif len(json_reply['event_list']) == 0:
                self.logger.info(
                    'no events... servers down? timestamp: {}'.format(timestamp))
            else:
                """
                  the api can return the usual killboard
                  or some kills while the servers are down

                  does it matter the servers are down?
                  how to find out the servers are down? we can compare timestamps
                  the latest event of the new event_list will have the same timestamp
                  as old_timestamp. the 0 event case is already
                  handled. kills in locked servers can happen, but they should have
                  a new timestamp too.
                """

                # use the fact that we request a sorted event_list by timestamp
                # event_list is sorted in decreasing order, so we iterate it backwards
                # logger.debug(json_reply['event_list'][0]['timestamp'])  #to (larger, later)
                # logger.debug(json_reply['event_list'][-1]['timestamp'])
                # #from (smaller, earlier)

                # we'll reset to 0 and find the time of the last kill
                # need the timestamp for after= argument when we request
                # killboard
                timestamp = 0
                must_commit = False
                for event in reversed(json_reply['event_list']):
                    # self.logger.debug(type(json_reply['event_list']))

                    try:

                        # find latest timestamp, even for suicides
                        # even when theres only an attacker or loser
                        newtimestamp = int(event['timestamp'])
                        timestamp = max(timestamp, newtimestamp)

                        # get nicks of duelers from the json
                        attacker = event['attacker']['name']['first']
                        attacker_br = int(
                            event['attacker']['battle_rank']['value'])
                        attacker_faction = event['attacker']['faction']['code_tag']

                        loser = event['character']['name']['first']
                        loser_br = int(
                            event['character']['battle_rank']['value'])
                        loser_faction = event['character']['faction']['code_tag']

                        world = event['world']['name']['en']

                        # can't play against yourself
                        if attacker != loser:

                            # find nicks in the database or insert them with
                            # default values
                            attacker_rating, attacker_rd, attacker_volatility = self.find_nick_or_default(attacker)
                            loser_rating, loser_rd, loser_volatility = self.find_nick_or_default(loser)

                            # glicko 1v1
                            att_glicko_rat = self.glicko.create_rating(
                                attacker_rating, attacker_rd, attacker_volatility)
                            lose_glicko_rat = self.glicko.create_rating(
                                loser_rating, loser_rd, loser_volatility)

                            try:
                                new_att, new_lose = self.glicko.rate_1vs1(
                                    att_glicko_rat, lose_glicko_rat)
                                # print(new_att.mu, new_att.phi, new_lose.mu, new_lose.phi)

                                # update rating,rd etc
                                # self.logger.debug('att {} loser {}'.format(attacker, loser))

                                # rating
                                assert 0 < new_att.mu < 3000, 'wtf attacker rating'
                                assert 0 < new_lose.mu < 3000, 'wtf loser rating'

                                # volatility
                                assert 0.01 < new_att.sigma < 1.2, 'wtf attacker volatility'
                                assert 0.01 < new_lose.sigma < 1.2, 'wtf loser volatility'

                                must_commit = True
                                self.conn_ctx.cursor.execute(
                                    self.update_qry,
                                    (new_att.mu,
                                        new_att.phi,
                                        new_att.sigma,
                                        newtimestamp,
                                        world,
                                        attacker_faction,
                                        attacker_br,
                                        attacker))
                                # logger.info(cursor.statement)
                                self.conn_ctx.cursor.execute(
                                    self.update_qry,
                                    (new_lose.mu,
                                        new_lose.phi,
                                        new_lose.sigma,
                                        newtimestamp,
                                        world,
                                        loser_faction,
                                        loser_br,
                                        loser))
                                # self.logger.debug('updating with stmt ' + self.conn_ctx.cursor.statement)
                            except OverflowError as e:
                                self.logger.error('{} attacker: {} {}; loser: {} {}; json: {}'.format(str(e), attacker, att_glicko_rat, loser, lose_glicko_rat, str(json_reply)))
                            except AssertionError as e:
                                self.logger.error('{} attacker: {} {}; loser: {} {}; json: {}'.format(str(e), attacker, att_glicko_rat, loser, lose_glicko_rat, str(json_reply)))
                        else:
                            #self.logger.debug('killed self {} {}'.format(attacker, loser))
                            pass

                    except KeyError as e:
                        """
                           sometimes a new character is killed or kills someone, in this
                           case the api returns the data only about the attacked or
                           the attacker, not both.

                           sometimes the world info is unavailable too
                           in both cases we will skip the event and go on
                        """

                        # logger.error(str(e) + ' ' + repr(e))
                        for err in e.args:
                            # logger.error(err)
                            if err not in {'attacker', 'character', 'world'}:
                                self.logger.error(
                                    'key not found {}; event is {}'.format(
                                        err, event))
                        # logger.error(event)
                        # nothing else to do, return the timestamp eventually
                        pass

                """
                I rather stop and investigate API bugs and updates.
                maintaining validation code for every update won't be any
                more robust. Given the quality of documentation and the TOS,
                they expect you to reverse engineer their work without much
                warning
                """
                if timestamp == 0:
                    self.logger.error('json_reply: {}'.format(json_reply))
                assert timestamp != 0, 'why is this still 0, the api should have returned something'

                if 'event_list' in json_reply:
                    num_events = len(json_reply['event_list'])

                # we have to commit even after selects
                self.conn_ctx.commit()
                self.logger.debug('commit')

        except KeyError as e:
            self.logger.error('wtf some key error')
            self.logger.error(str(json_reply))

        return timestamp, num_events


def main(conf):
    user = conf['user']
    password = conf['password']
    host = conf['host']
    database = conf['database']
    serviceid = conf['serviceid']
    useragent = conf['useragent']

    ioloop = tornado.ioloop.IOLoop.instance()

    logger = setup_logging()
    conn_ctx = SqlConnectionCtx(user, password, host, database, logger)

    populate_db = Populate_db(ioloop, serviceid, useragent, logger, conn_ctx)
    cb = populate_db.select_update_tick
    ioloop.add_timeout(datetime.timedelta(seconds=1), cb)

    delete_inactive = DeleteInactive(ioloop, logger, conn_ctx)
    cb = delete_inactive.delete_tick
    ioloop.add_timeout(datetime.timedelta(seconds=1), cb)

    summary_tbl_update = SummaryTableUpdate(ioloop, logger, conn_ctx)
    cb = summary_tbl_update.avg_player_rating_tick
    ioloop.add_timeout(datetime.timedelta(seconds=1), cb)

    try:
        ioloop.start()
    except KeyboardInterrupt:
        ioloop.stop()

    conn_ctx.close()
