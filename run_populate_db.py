import argparse
import json

import ps2ranks.populate_db.populate_db

def read_config(conf_path):
    with open(conf_path) as f:
        config = json.loads(f.read())
    return config

parser = argparse.ArgumentParser()
parser.add_argument('CONFIGPATH', type=str, help='path to secrets.json')
args = parser.parse_args()
conf = read_config(args.CONFIGPATH)


ps2ranks.populate_db.populate_db.main(conf)