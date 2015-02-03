This is the leaderboard accessible on http://melbalabs.com and http://ps2ranks.brb.dj

There are backend and frontend parts. I'm posting just the backend as it's
less site specific and more interesting. 

The backend polls the game census APIs for the killboard and uses it to rate; 
create; update; remove inactive characters.  
The frontend shows information about the leaderboard; a top 1000 and allows
rating and rank searches both global and by server.  

Built with glicko 2, python 3, matplotlib, tornado, requests, cherrypy, 
bootstrap, mako, nginx, uwsgi, mysql.  

There's a lurking bug somewhere that happens once in a blue moon 
(from months to years apart), where a player gets silly rating and deviation.