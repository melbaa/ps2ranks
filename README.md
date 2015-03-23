This is the leaderboard accessible on http://melbalabs.com
The humble beginnings can be traced back to a reddit announcement back in 
early 2014
http://www.reddit.com/r/Planetside/comments/1nvbp0/planetside_2_leaderboard_gg_uninstall_edition/

There are backend and frontend parts.

The backend polls the game census APIs for the killboard and uses it to rate; 
create; update; remove inactive characters. To configure it, you'll need to
create a config file by following the example in populate_db/. It asks for
things such as mysql user/pass/addr/database and API service id and user
agent. It's built with glicko 2, python 3, tornado, requests, mysql.  
 
The frontend shows information about the leaderboard, such as active users,
average rating, time since last update, a top 1000. It allows
rating and rank searches both global and by server. It uses nginx, cherrypy,
bootstap, mako, uwsgi, matplotlib.

There's a lurking bug somewhere that happens once in a blue moon 
(from months to years apart), where a player gets silly rating and deviation.