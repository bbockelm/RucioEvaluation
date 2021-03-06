#! /bin/env python
# Script for creating RSEs and updating their attributes.
# Initially will use PhEDEx nodes information as input,
# should be easily transformed/extended for using other sources.

import argparse
import logging
import sys
import pprint

import urlparse
import requests
import json
import re

from functools import wraps

from rucio.client.accountclient import AccountClient
from rucio.client.rseclient import RSEClient
from rucio.common.exception import Duplicate, RSEProtocolPriorityError, \
    RSEProtocolNotSupported, RSENotFound, InvalidObject, CannotAuthenticate

# Create reusable session:
session = requests.Session()
session.verify = '/etc/grid-security/certificates'

DATASVC_URL = 'http://cmsweb.cern.ch/phedex/datasvc/json/prod/'
# Pre-compiled regex for PhEDEx returned data:
prog = re.compile('.* -service (.*?) .*')
gsiftp_scheme = re.compile('(gsiftp)://(.+?):?(\d+)?/?(/.*)')
srm_scheme = re.compile('(srm)://(.+?):(\d+)?(.*=)?(/.*)')

# To exclude RSEs that already have defined protocols and scheme:
exclude_rse = (
    'T2_FR_GRIF_IRFU',
    'T2_FR_GRIF_LLR',
    'T2_FR_GRIF_LLR_preprod',
    'T2_IT_PISA_NRTESTING',
    'T2_US_NEBRASKA_SCRATCHDISK',
    'T2_US_NEBRASKA_USERDISK',
    'T2_US_UCSD',
    'T3_IT_PERUGIA',
)


def setup_logger(logger):
    """ Code borrowed from bin/rucio-admin
    """
    logger.setLevel(logging.DEBUG)
    hdlr = logging.StreamHandler()

    def emit_decorator(fcn):
        def func(*args):
            if True:
                formatter = logging.Formatter("%(message)s")
            else:
                levelno = args[0].levelno
                if levelno >= logging.CRITICAL:
                    color = '\033[31;1m'
                elif levelno >= logging.ERROR:
                    color = '\033[31;1m'
                elif levelno >= logging.WARNING:
                    color = '\033[33;1m'
                elif levelno >= logging.INFO:
                    color = '\033[32;1m'
                elif levelno >= logging.DEBUG:
                    color = '\033[36;1m'
                else:
                    color = '\033[0m'
                formatter = logging.Formatter(
                    '{0}%(asctime)s ' +
                    '%(levelname)s ' +
                    '\[%(message)s]\033[0m'.format(color))
            hdlr.setFormatter(formatter)
            return fcn(*args)
        return func
    hdlr.emit = emit_decorator(hdlr.emit)
    logger.addHandler(hdlr)


def exception_handler(function):
    """Code borrowed from bin/rucio-admin
    """
    @wraps(function)
    def new_funct(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except InvalidObject as error:
            logger.error(error)
            return error.error_code
        except CannotAuthenticate as error:
            logger.error(error)
            sys.exit(1)
        except Duplicate as error:
            logger.error(error)
            return error.error_code
    return new_funct

# Functions for getting PhEDEx information:


def PhEDEx_node_exists(node):
    """Check existence"""
    if node in PhEDEx_node_names():
        return True
    else:
        return False


def PhEDEx_node_to_RSE(node):
    """ Translates PhEDEx node names to RSE names.
    Make sure new names comply with the policies defined in:
    ./lib/rucio/common/schema/cms.py
    ./lib/rucio/core/permission/cms.py
    Because once created RSE name can't be reused, allow to postpend
    the name with a test_tag string (default: 0000).
    In reality something like USERDISK|DATADISK|SCRATCHDISK will be used.
    """
    if args.suffix:
        node = node + '_' + args.suffix
    return node.upper()


def PhEDEx_node_FTS_servers(node):
    """Returns a list of FTS servers from node's FileDownload agent config"""
    # FIXME: check node existence.
    payload = {'agent': 'FileDownload', 'node': node}
    URL = urlparse.urljoin(DATASVC_URL, 'agentlogs')
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    servers = {}
    for agent in DATA['phedex']['agent']:
        for log in agent['log']:
            for message in log['message'].values():
                if ('-backend FTS3' in message):
                    result = prog.match(message)
                    if result:
                        servers[result.group(1)] = True
    return servers.keys()

# Functions for translating information to Rucio standards


def PhEDEx_node_names():
    """ Returns a sorted list of PhEDEx node names via data service nodes API
    excluding nodes with no data. """
    URL = urlparse.urljoin(DATASVC_URL, 'nodes')
    payload = {'noempty': 'y'}
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    names = []
    for n in DATA['phedex']['node']:
        names.append(n['name'])
    names.sort()
    return names


def PhEDEx_links():
    """ Get a list of all links between PhEDEx nodes.
    Filter by status=OK and kind=WAN """
    URL = urlparse.urljoin(DATASVC_URL, 'links')
    payload = {'status': 'ok', 'kind': 'WAN'}
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    links = []
    for link in DATA['phedex']['link']:
        if link['kind'] == 'WAN' and link['status'] == 'ok':
            links.append(link)
    return links


def PhEDEx_node_protocol_PFN(node, protocol='srmv2', lfn='/store/test/rucio'):
    """ Returns a PFN for CMS top namespace for a given node/protocol pair """
    URL = urlparse.urljoin(DATASVC_URL, 'lfn2pfn')
    payload = {'node': node, 'protocol': protocol, 'lfn': lfn}
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    return DATA['phedex']['mapping'][0]['pfn']


def PhEDEx_node_protocols(node):
    """ Returns a sorted list of protocols defined in a node's tfc """
    URL = urlparse.urljoin(DATASVC_URL, 'tfc')
    payload = {'node': node}
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    protocols = []
    for p in DATA['phedex']['storage-mapping']['array']:
        protocols.append(p['protocol'])
    for p in sorted(set(protocols)):    # Eliminate duplicates
        print ("%s %s %s" % (node, p, PhEDEx_node_protocol_PFN(node, p)))
    return sorted(set(protocols))


def PhEDEx_link_attributes(source, dest):
    """ Returns values of various PhEDEx link attributes"""
    URL = urlparse.urljoin(DATASVC_URL, 'links')
    payload = {'from': source, 'to': dest}
    RESP = session.get(url=URL, params=payload)
    DATA = json.loads(RESP.content)
    if DATA['phedex']['link']:
        return DATA['phedex']['link'][0]
    else:
        return None


def PFN_to_protocol_attributes(pfn):
    proto = {}
    if srm_scheme.match(pfn):
        (scheme, hostname, port, web_service_path, prefix) = \
            srm_scheme.match(pfn).groups()
        proto = {'hostname': hostname,
                 'scheme': scheme,
                 'impl': 'rucio.rse.protocols.gfalv2.Default',
                 'prefix': prefix,
                 'port': port}
        if port:
            proto['port'] = port
        proto['extended_attributes'] = {'space_token': None,
                                        'web_service_path': web_service_path}
    if gsiftp_scheme.match(pfn):
        (scheme, hostname, port, prefix) = gsiftp_scheme.match(pfn).groups()
        proto = {'hostname': hostname,
                 'scheme': scheme,
                 'impl': 'rucio.rse.protocols.gfalv2.Default',
                 'prefix': prefix,
                 'port': port}
        proto['port'] = '0' if not port else port
    return proto

# Functions involving Rucio client actions


@exception_handler
def whoami(account='natasha', auth_type='x509_proxy'):
    """Runs whoami command for a given account via client tool,
    requires a valid proxy
    """
    account_client = AccountClient(account=account, auth_type='x509_proxy')
    print("Connected to rucio as %s" % account_client.whoami()['account'])


@exception_handler
def list_rses():
    """Prints names of existing RSEs"""
    for rse in rse_client.list_rses():
        print (rse['rse'])


@exception_handler
def get_rse_distance(source, dest):
    """Prints distance between two RSEs"""
    return rse_client.get_distance(source, dest)


@exception_handler
def set_rse_ftsserver(rse, server='https://fts3.cern.ch:8446'):
    """ Adds fts server to an existing RSE , use CERN server by default"""
    if args.dry_run:
        print "DRY RUN: adding fts server "+server+" to RSE: "+rse
        return
    rse_client.add_rse_attribute(rse=rse, key='fts', value=server)


@exception_handler
def set_rse_distance_ranking(source, dest, value):
    if args.dry_run:
        print "DRY RUN: set distance and ranking from " + source + " to " + \
            dest + " to: " + str(value)
        return
    # FIXME: update distance if already exists
    # Set both distance and ranking to the same value of PhEDEx link distance:
    params = {'distance': int(value), 'ranking': int(value)}
    rse_client.add_distance(source, dest, params)


@exception_handler
def set_rse_protocol(rse, node):
    """ Gets protocol used for PhEDEx transfers at the node,
    identifies the corresponding RSE protocol scheme and parameters
    adds resulting protocol to a given existing rse
    Set lfn2pfn_algorithm attribute"""
    algo = 'identity'
    pfn = PhEDEx_node_protocol_PFN(node)
    proto = PFN_to_protocol_attributes(pfn)
    # Allow remote read/write/delete access:
    proto['domains'] = {"wan": {"read": 1, "write": 1, "delete": 1,
                        "third_party_copy": 1}}
    if args.dry_run:
        print "DRY RUN: set protocol for " + rse
        print "DRY RUN: set " + rse + " lfn2pfn_algorithm attribute to " + algo
        print "DRY RUN: set protocol: "
        pprint.pprint(proto)
        return

    rse_client.add_rse_attribute(rse=rse, key='lfn2pfn_algorithm', value=algo)
    rse_client.add_protocol(rse, proto)


@exception_handler
def add_rse(name):
    """Adds an rse """
    if args.dry_run:
        print "DRY RUN: adding RSE: " + name
        return
    rse_client.add_rse(name)
    if args.verbose:
        print "Added RSE "+name
        info = rse_client.get_rse(name)
        for q, v in info.iteritems():
            print q+" :  ", v


@exception_handler
def update_rse(rse, node):
    # Exclude RSEs that are already manually set up
    if rse in exclude_rse:
        print "update_rse: skip excluded " + rse
        return

    add_rse(rse)
    set_rse_ftsserver(rse)  # proxy delegation is enabled only for this server
    set_rse_protocol(rse, node)
    # Update all to/from links here ???

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='''- create or update RSEs and their attributes
        based on TMDB information''',
        epilog="""This is a test version use with care!"""
        )
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='increase output verbosity')
    parser.add_argument('-t', '--dry-run', action='store_true',
                        help='only printout what would have been done')
    parser.add_argument('--test-auth', action='store_true',
                        help='''executes AccountClient.whoami,
                        use --account option to change the identity''')
    parser.add_argument('--list-nodes', action='store_true',
                        help='list PhEDEx node names')
    parser.add_argument('--list-rses', action='store_true',
                        help='list RSE names')
    parser.add_argument('--account', default='natasha', help=' use account ')
    parser.add_argument('--update-all',  action='store_true',
                        help="""create or update RSEs for all PhEDEx nodes,
                        nodes with no data will be ignored.""")
    parser.add_argument('--update-link', metavar=('FROM_NODE', 'TO_NODE'),
                        nargs=2, help="""create or update a given link""")
    parser.add_argument('--update-all-links', action='store_true',
                        help="""create or update all links""")
    parser.add_argument('--update-rse', metavar=('RSE', 'NODE'),
                        nargs=2, help="""create or update existing RSE using
                        PhEDEx node name and configuration, nodes with no data
                        will be ignored
                        """)
    parser.add_argument('--get-rse-distance',
                        metavar=('SOURCE', 'DESTINATION'),
                        nargs=2,
                        help='get distance between two RSEs')
    parser.add_argument('--link-attributes',
                        metavar=('SOURCE', 'DESTINATION'),
                        nargs=2,
                        help='get PhEDEx link attributes')
    parser.add_argument('--suffix', default='nrtesting',
                        help='''append suffix to RSE names pre-generated
                        from PhEDEx node names''')
    parser.add_argument('--node-fts-servers',
                        default=None,
                        metavar='NODE',
                        help='list fts servers used by PhEDEx node')
    parser.add_argument('--node-protocols', metavar='NODE',
                        help="list all protocols defined in the node's TFC")
    parser.add_argument('--node-pfn', metavar='NODE',
                        help="""get PFN for /store/test/rucio and
                        srmv2 protocol as defined in node's TFC""")
    args = parser.parse_args()
    if args.verbose:
        print (args)
    # Take care of Rucio exceptions:
    logger = logging.getLogger("user")
    setup_logger(logger)

    # Handle PhEDEx queries:
    if args.node_pfn:
        node = args.node_pfn
        if not PhEDEx_node_exists(node):
            print "Unknown node: " + node
            sys.exit(2)
        pfn = PhEDEx_node_protocol_PFN(node)
        proto = PFN_to_protocol_attributes(pfn)
        print "=== NODE:  " + node + "\n=== PFN:   " + pfn + "\n=== PROTO: "
        pprint.pprint(proto)
        sys.exit()

    if args.list_nodes:
        nodes = PhEDEx_node_names()
        for n in nodes:
            print n
        sys.exit()

    if args.node_fts_servers:
        servers = PhEDEx_node_FTS_servers(args.node_fts_servers)
        print "FTS servers used by " + args.node_fts_servers + ' PhEDEx node:'
        for s in servers:
            print s
        sys.exit()

    if args.node_protocols:
        PhEDEx_node_protocols(args.node_protocols)
        sys.exit()

    if args.test_auth:
        whoami(account=args.account)

    # create re-usable RSE client connection:
    rse_client = RSEClient(account=args.account, auth_type='x509_proxy')

    if args.list_rses:
        list_rses()

    if args.get_rse_distance:
        (s, d) = args.get_rse_distance
        pprint.pprint(get_rse_distance(s, d))
        sys.exit()

    if args.link_attributes:
        (s, d) = args.link_attributes
        pprint.pprint(PhEDEx_link_attributes(s, d))
        sys.exit()

    # Handle RSE additions and configuration update

    if args.update_rse:
        (rse, node) = args.update_rse
        update_rse(rse, node)

    if args.update_all:
        for n in PhEDEx_node_names():
            # Use PhEDEx_node_to_RSE(n) to use custom RSE names rather than
            # real PhEDEx node names
            update_rse(n, n)

    # Handle links:

    if args.update_link:
        (s, d) = args.update_link
        info = PhEDEx_link_attributes(s, d)
        set_rse_distance_ranking(s, d, info['distance'])

    if args.update_all_links:
        for link in PhEDEx_links():
            set_rse_distance_ranking(
                                    link['from'],
                                    link['to'],
                                    link['distance']
                                    )

