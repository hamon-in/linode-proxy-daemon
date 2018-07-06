"""
Script to auto-rotate and configure Linodes as squid proxies.

"""

import argparse
import os
import sys
import time
import random
import collections
import operator
import uuid
import threading
import signal
import json
import email_report

from utils import daemonize, randpass, enum, LiCommand, AWSCommand
from botocore.exceptions import ClientError

# Rotation Policies
Policy = enum('ROTATION_RANDOM',
              # Least recently used
              'ROTATION_LRU',
              # Switch to another region
              'ROTATION_NEW_REGION',
              # LRU + New region
              'ROTATION_LRU_NEW_REGION')

region_dict = {2: 'Dallas',
               3: 'Fremont',
               4: 'Atlanta',
               6: 'Newark',
               7: 'London',
               8: 'Tokyo',
               9: 'Singapore',
               10: 'Frankfurt'}


email_template = """

I just switched a proxy node in the proxy infrastructure. Details are below.

In: %(label)s, %(proxy_in)s
Out: %(label)s, %(proxy_out)s

Region: %(region)s

-- Linode proxy daemon

"""

# Post process command
post_process_cmd_template = """ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %s@%s "%s" """
iptables_restore_cmd = "sudo iptables-restore < /etc/iptables.rules"
squid_restart_cmd = "sudo squid3 -f /etc/squid3/squid.conf"

class ProxyConfig(object):
    """ Class representing configuration of crawler proxy infrastructure """

    def __init__(self, cfg='proxy.conf'):
        """ Initialize proxy config from the config file """

        self.parse_config(cfg)
        # This is a file with each line of the form
        # IPV4 address, datacenter code, linode-id, switch_in timestamp, switch_out timestamp
        # E.g: 45.79.91.191, 3, 1446731065, 144673390
        try:
            proxies = map(lambda x: x.strip().split(','), open(self.proxylist).readlines())
            # Proxy IP to (switch_in, switch_out) timestamp mappings
            self.proxy_dict = {}
            # Proxy IP to enabled mapping
            self.proxy_state = {}
            self.process_proxies(proxies)
        except (OSError, IOError), e:
            print e
            sys.exit("Fatal error, proxy list input file " + self.proxylist + " not found!")

        try:
            self.proxy_template = open(self.lb_template).read()
        except (OSError, IOError), e:
            print e
            sys.exit("Fatal error, template config input file " + template_file + " not found!")

    def parse_config(self, cfg):
        """ Parse the configuration file and load config """

        self.config = json.load(open(cfg))
        for key,value in self.config.items():
            # Set attribute locally
            setattr(self, key, value)

        # Do some further processing
        self.frequency = float(self.frequency)*3600.0
        self.policy = eval('Policy.' + self.policy)

    def get_proxy_ips(self):
        """ Return all proxy IP addresses as a list """

        return self.proxy_state.keys()

    def get_active_proxies(self):
        """ Return a list of all active proxies as a list """

        return map(self.proxy_dict.get, filter(self.proxy_state.get, self.proxy_state.keys()))

    def process_proxies(self, proxies):
        """ Process the proxy information to create internal dictionaries """

        # Prepare the proxy region dict
        for proxy_ip, region, proxy_id, switch_in, switch_out in proxies:
            # If switch_in ==0: put current time
            if int(float(switch_in))==0:
                switch_in = int(time.time())
            if int(float(switch_out))==0:
                switch_out = int(time.time())

            if self.vps_provider == 'linode':
                self.proxy_dict[proxy_ip] = [proxy_ip, region, proxy_id, int(float(switch_in)), int(float(switch_out))]
            elif self.vps_provider == 'aws':
                self.proxy_dict[proxy_ip] = [proxy_ip, int(region), proxy_id, int(float(switch_in)), int(float(switch_out))]
            self.proxy_state[proxy_ip] = True

        print 'Processed',len(self.proxy_state),'proxies.'

    def get_proxy_for_rotation(self,
                               use_random=False,
                               least_used=False,
                               region_switch=False,
                               input_region=3):
        """ Return a proxy IP address for rotation using the given settings. The
        returned proxy will be replaced with a new proxy.

        @use_random - Means returns a random proxy from the current active list
        @least_used - Returns a proxy IP which is the oldest switched out one
        so we keep the switching more or less democratic.
        @region_switch - Returns a proxy which belongs to a different region
        from the new proxy.
        @input_region - The region of the new proxy node - defaults to Fremont, CA.

        Note that if use_random is set to true, the other parameters are ignored.

        """

        active_proxies = self.get_active_proxies()
        print 'Active proxies =>',active_proxies

        if use_random:
            # Pick a random proxy IP
            proxy = random.choice(active_proxies)
            print 'Returning proxy =>',proxy
            proxy_ip = proxy[0]

            # Remove it from every data structure
            self.switch_out_proxy(proxy_ip)
            return proxy

        if least_used:
            # Pick the oldest switched out proxy i.e one
            # with smallest switched out value
            proxies_used = sorted(active_proxies,
                                  key=operator.itemgetter(-1))

            print 'Proxies used =>',proxies_used

            if region_switch:
                # Find the one with a different region from input
                for proxy, reg, pi, si, so in proxies_used:
                    if reg != input_region:
                        print 'Returning proxy',proxy,'from region',reg
                        self.switch_out_proxy(proxy)
                        return proxy

            # If all regions are already in use, pick the last used
            # proxy anyway
            return proxies_used[0][0]

        if region_switch:
            # Pick a random proxy not in the input region
            proxies = active_proxies
            random.shuffle(proxies)

            for proxy, reg, pi, si, so in proxies:
                if reg != input_region:
                    print 'Returning proxy',proxy,'from region',reg
                    self.switch_out_proxy(proxy)
                    return proxy

    def __getattr__(self, name):
        """ Return from local, else written from config """

        try:
            return self.__dict__[name]
        except KeyError:
            return self.config.get(name)

    def switch_out_proxy(self, proxy):
        """ Switch out a given proxy IP """

        # Disable it
        self.proxy_state[proxy] = False
        # Mark its switched out timestamp
        self.proxy_dict[proxy][-1] = int(time.time())

    def switch_in_proxy(self, proxy, proxy_id, region):
        """ Switch in a given proxy IP """

        # Mark its switched out timestamp
        if self.vps_provider == 'linode':
            self.proxy_dict[proxy] = [proxy, region, proxy_id, int(time.time()), int(time.time())]
        elif self.vps_provider == 'aws':
            self.proxy_dict[proxy] = [proxy, int(region), proxy_id, int(time.time()), int(time.time())]
            # Enable it
        self.proxy_state[proxy] = True

    def get_active_regions(self):
        """ Return unique regions for which proxies are active """

        regions = set()
        for proxy,region,pi,si,so in self.proxy_dict.values():
            if self.proxy_state[proxy]:
                regions.add(region)

        return list(regions)

    def write(self, disabled=False):
        """ Write current state to an output file """

        lines = []
        for proxy, reg, pi, si, so in self.proxy_dict.values():
            if disabled or self.proxy_state[proxy]:
                lines.append('%s,%s,%s,%s,%s\n' % (proxy, str(reg), str(pi), str(int(si)), str(int(so))))

        open(self.proxylist,'w').writelines(lines)

    def write_lb_config(self, disabled=False, test=False):
        """ Write current proxy configuration into the load balancer config """

        lines, idx = [], 1
        # Shuffle
        items = self.proxy_dict.values()
        for i in range(10):
            random.shuffle(items)

        for proxy, reg, pi, si, so in items:
            if self.proxy_state[proxy]:
                lines.append('\tserver  squid%d %s:8321 check inter 10000 rise 2 fall 5' % (idx, proxy))
                idx += 1

        squid_config = "\n".join(lines)
        content = self.proxy_template % locals()
        # Write to temp file
        tmpfile = '/tmp/.haproxy.cfg'
        open(tmpfile,'w').write(content)

        # If running in test mode, don't do this!
        if not test:
            # Run as sudo
            cmd = 'sudo cp %s %s; rm -f %s' % (tmpfile, self.lb_config, tmpfile)
            os.system(cmd)

        self.reload_lb()
        return True

    def reload_lb(self):
        """ Reload the HAProxy load balancer """

        return (os.system(self.lb_restart) == 0)

    def get_proxy_id(self, proxy):
        """ Given proxy return its id """

        return self.proxy_dict[proxy][2]

    def get_email_config(self):
        """ Return email configuration """

        return self.config['email']

class ProxyRotator(object):
    """ Proxy rotation, provisioning & re-configuration with linode nodes """

    def __init__(self, cfg='proxy.conf', test_mode=False, rotate=False, region=None):
        self.config = ProxyConfig(cfg=cfg)
        print 'Frequency set to',self.config.frequency,'seconds.'
        # Test mode ?
        self.test_mode = test_mode
        # Event object
        self.alarm = threading.Event()
        # Clear the event
        self.alarm.clear()
        # Heartbeat file
        self.hbf = '.heartbeat'
        # Linode creation class
        self.linode_cmd = LiCommand(config=self.config)
        #AWS resource manager
        self.aws_command = AWSCommand(config=self.config)
        # If rotate is set, rotate before going to sleep
        if rotate:
            print 'Rotating a node'
            self.rotate(region=region)

        signal.signal(signal.SIGTERM, self.sighandler)
        signal.signal(signal.SIGUSR1, self.sighandler)

    def pick_region(self):
        """ Pick the region for the new node """

        # Try and pick a region not present in the
        # current list of nodes
        regions = self.config.get_active_regions()
        # Shuffle current regions
        random.shuffle(self.config.region_ids)

        for reg in self.config.region_ids:
            if reg not in regions:
                return reg

        # All regions already present ? Pick a random one.
        return random.choice(self.config.region_ids)

    def make_new_linode(self, region, test=False, verbose=False):
        """ Make a new linode in the given region """

        # If calling as test, make up an ip
        if test:
            return '.'.join(map(lambda x: str(random.randrange(20, 100)), range(4))), random.randrange(10000,
                                                                                                       50000)



        print 'Making new linode ','...'
        new_linode, password = self.linode_cmd.create_li()

        # data = os.popen(cmd).read()
        if verbose:
            print new_linode
        # The IP is the last line of the command
        ip = new_linode.ipv4[0]
        # Proxy ID
        pid = new_linode.id
        print 'I.P address of new linode is',ip
        print 'ID of new linode is',pid
        # Post process the host
        print 'Post-processing',ip,'...'
        self.post_process(ip)
        return ip, pid

    def rotate(self, region=None):
        """ Rotate the configuration to a new node """

        proxy_out_label = None
        # Pick the data-center
        if region == None:
            print 'Picking a region ...'
            region = self.pick_region()
        else:
            print 'Using supplied region',region,'...'

        if self.config.vps_provider == 'linode':
        # Switch in the new linode from this region
            new_proxy, proxy_id = self.make_new_linode(region)

        elif self.config.vps_provider == 'aws':
        # Switch in the new aws instance
            new_proxy, proxy_id = self.make_new_ec2()

        # Rotate another node
        if self.config.policy == Policy.ROTATION_RANDOM:
            proxy_out = self.config.get_proxy_for_rotation(use_random=True, input_region=region)
        elif self.config.policy == Policy.ROTATION_NEW_REGION:
            proxy_out = self.config.get_proxy_for_rotation(region_switch=True, input_region=region)
        elif self.config.policy == Policy.ROTATION_LRU:
            proxy_out = self.config.get_proxy_for_rotation(least_used=True, input_region=region)
        elif self.config.policy == Policy.ROTATION_LRU_NEW_REGION:
            proxy_out = self.config.get_proxy_for_rotation(least_used=True, region_switch=True,
                                                        input_region=region)

        # Switch in the new proxy
        self.config.switch_in_proxy(new_proxy, proxy_id, 0)
        print 'Switched in new proxy',new_proxy
        # Write configuration
        self.config.write()
        print 'Wrote new configuration.'
        # Write new HAProxy LB template and reload ha proxy
        ret1 = self.config.write_lb_config()
        ret2 = self.config.reload_lb()
        if ret1 and ret2:
            if proxy_out != None:
                print 'Switched out proxy',proxy_out
                proxy_out_id = self.config.get_proxy_id(proxy_out)
                if proxy_out_id != 0:
                    if self.config.vps_provider == 'linode':
                        proxy_out_label = self.linode_cmd.get_label(proxy_out_id)
                        print 'Removing switched out linode',proxy_out_id
                        self.linode_cmd.delete_linode(int(proxy_out_id))
                    elif self.config.vps_provider == 'aws':
                        print 'Removing switched out aws instance',proxy_out_id
                        self.aws_command.delete_ec2(proxy_out_id)
                else:
                    'Proxy id is 0, not removing proxy',proxy_out
        else:
            print 'Error - Did not switch out proxy as there was a problem in writing/restarting LB'

        if self.config.vps_provider == 'linode':
            if proxy_out_label != None:
                # Get its label and assign it to the new linode
                print 'Assigning label',proxy_out_label,'to new linode',proxy_id
                time.sleep(5)
                #self.linode_cmd.linode_update(int(proxy_id),
                #                            proxy_out_label,
                #                            self.config.group)

        # Post process the host
        print 'Post-processing',new_proxy,'...'
        self.post_process(new_proxy)
        self.send_email(proxy_out, proxy_out_label, new_proxy, region)

    def send_email(self, proxy_out, label, proxy_in, region):
        """ Send email upon switching of a proxy """

        print 'Sending email...'
        region = region_dict[region]
        content = email_template % locals()
        email_config = self.config.get_email_config()

        email_report.email_report(email_config, "%s", content)

    def post_process(self, ip):
        """ Post-process a switched-in host """

        # Sleep a bit before sshing
        time.sleep(5)
        cmd = post_process_cmd_template % (self.config.user, ip, iptables_restore_cmd)
        print 'SSH command 1=>',cmd
        os.system(cmd)
        cmd = post_process_cmd_template % (self.config.user, ip, squid_restart_cmd)
        print 'SSH command 2=>',cmd
        os.system(cmd)

    def alive(self):
        """ Return whether I should be alive """

        return os.path.isfile(self.hbf)

    def create(self, region=3):
        """ Create a new linode for testing """
        if self.config.vps_provider == 'linode':
            print 'Creating new linode in region',region,'...'
            new_proxy = self.make_new_linode(region=None, verbose=True)
        elif self.config.vps_provider == 'aws':
            print 'Creating new ec2 instance','...'
            new_proxy = self.make_new_ec2()

    def drop(self):
        """ Drop all the proxies in current configuration (except the LB) """

        if self.config.vps_provider == 'linode':
            print 'Dropping all proxies ...'
            proxies = rotator.linode_cmd.list_instances()
            for item in proxies:
                ip,dc,lid,si,so = (item.ipv4[0], item.region.id, item.id,0,0)
                print '\tDropping linode',lid,'with IP',ip,'from dc',dc,'...'
                rotator.linode_cmd.delete_linode(lid)

        elif self.config.vps_provider == 'aws':
            print 'Dropping all proxies ...'
            proxies = rotator.aws_command.list_proxies()
            for item in proxies:
                ip,_,instance_id = item.split(',')
                print '\tDropping ec2',instance_id,'with IP',ip,'...'
                self.aws_command.delete_ec2(instance_id)
        print 'done.'

    def provision(self, count=8, add=False):
        """ Provision an entirely fresh set of linodes after dropping current set """

        if not add:
            self.drop()

        num, idx = 0, 0

        # If we are adding Linodes without dropping, start from current count
        if add:
            start = len(self.config.get_active_proxies())
        else:
            start = 0

        for i in range(start, start + count):

            if self.config.vps_provider == 'linode':
                # region = self.pick_region()
                # Do a round-robin on regions
                region = self.config.region_ids[idx % len(self.config.region_ids) ]
                try:
                    ip, lid = self.make_new_linode(region)
                    # self.linode_cmd.linode_update(int(lid),
                    #                             self.config.proxy_prefix + str(i+1),
                    #                             self.config.group)
                    num += 1
                except Exception, e:
                    print 'Error creating linode',e

            elif self.config.vps_provider == 'aws':
                try:
                    ip, instance_id = self.make_new_ec2()
                    num += 1
                except ClientError as e:
                    print 'Error creating aws ec2 instance ',e

            idx += 1

        print 'Provisioned',num,' proxies.'
        if self.config.vps_provider == 'linode':
            proxies_list = rotator.linode_cmd.list_instances()
        elif self.config.vps_provider == 'aws':
            proxies_list = rotator.aws_command.list_proxies()
        # Randomize it
        for i in range(5):
            random.shuffle(proxies_list)

        proxies = list()
        for item in proxies_list:
            proxies.append(",".join((str(item.ipv4[0]), str(item.region.id), str(item.id),str(0),str(0))))

        print >> open('proxies.list', 'w'), '\n'.join(proxies)
        print 'Saved current proxy configuration to proxies.list'

    def test(self):
        """ Function to be called in loop for testing """

        proxy_out_label = ''
        region = self.pick_region()
        print 'Rotating proxy to new region',region,'...'
        # Make a test IP
        new_proxy, proxy_id = self.make_new_linode(region, test=True)
        proxy_out = self.config.get_proxy_for_rotation(least_used=True, region_switch=True,
                                                       input_region=region)

        if proxy_out != None:
            print 'Switched out proxy',proxy_out
            proxy_out_id = int(self.config.get_proxy_id(proxy_out))
            proxy_out_label = self.linode_cmd.get_label(proxy_out_id)

        # Switch in the new proxy
        self.config.switch_in_proxy(new_proxy, proxy_id, region)
        print 'Switched in new proxy',new_proxy
        # Write new HAProxy LB template and reload ha proxy
        self.config.write_lb_config(test=True)
        self.send_email(proxy_out, proxy_out_label, new_proxy, region)

    def stop(self):
        """ Stop the rotator process """

        try:
            os.remove(self.hbf)
            # Signal the event
            self.alarm.set()
            return True
        except (IOError, OSError), e:
            pass

        return False

    def sighandler(self, signum, stack):
        """ Signal handler """

        # This will be called when you want to stop the daemon
        self.stop()

    def run(self, daemon=True):
        """ Run as a background process, rotating proxies """

        # Touch heartbeat file
        open(self.hbf,'w').write('')
        # Fork
        if daemon:
            print 'Daemonizing...'
            daemonize('rotator.pid',logfile='rotator.log', drop=True)
        else:
            # Write PID anyway
            open('rotator.pid','w').write(str(os.getpid()))

        print 'Proxy rotate daemon started.'
        count = 1

        while True:
            # Wait on event object till woken up
            self.alarm.wait(self.config.frequency)
            status = self.alive()
            if not status:
                print 'Daemon signalled to exit. Quitting ...'
                break

            print 'Rotating proxy node, round #%d ...' % count
            if self.test_mode:
                self.test()
            else:
                self.rotate()
            count += 1

        sys.exit(0)
    # AWS code
    def make_new_ec2(self, test=False, verbose=False):
        # If calling as test, make up an ip
        if test:
            return '.'.join(map(lambda x: str(random.randrange(20, 100)), range(4))), random.randrange(10000,
                                                                                                       50000)
        params = dict(ImageId=self.config.aws_image_id,
                      InstanceType=self.config.aws_instance_type,
                      KeyName=self.config.aws_key_name,
                      SecurityGroupIds=self.config.aws_security_groups,
                      SubnetId=self.config.aws_subnet_id ,
                      DryRun=True)

        print 'Making new ec2...'
        ec2_instance = self.aws_command.create_ec2(**params)
        ec2_instance.wait_until_running()
        time.sleep(10)

        ip = ec2_instance.public_ip_address
        pid = ec2_instance.id

        # Post process the host
        print 'Post-processing',ip,'...'
        self.post_process(ip)

        return ip, pid

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='rotate_proxies')
    parser.add_argument('-C','--conf',help='Use the given configuration file', default='proxy.conf')
    parser.add_argument('-s','--stop',help='Stop the currently running daemon', action='store_true')
    parser.add_argument('-t','--test',help='Run the test function to test the daemon', action='store_true')
    parser.add_argument('-n','--nodaemon',help='Run in foreground', action='store_true',default=False)
    parser.add_argument('-c','--create',help='Create a proxy linode', action='store_true',default=False)
    parser.add_argument('-r','--region',help='Specify a region when creating a linode', default=3, type=int)
    parser.add_argument('-R','--rotate',help='Rotate a node immediately and go to sleep', default=False,
                        action='store_true')
    parser.add_argument('-D','--drop',help='Drop the current configuration of proxies (except LB)',
                        default=False,action='store_true')
    parser.add_argument('-P','--provision',help='Provision a fresh set of proxy linodes',default=False,
                        action='store_true')
    parser.add_argument('-A','--add',help='Add a new set of linodes to existing set',default=False,
                        action='store_true')
    parser.add_argument('-N','--num',help='Number of new linodes to provision or add (use with -P or -A)',type=int,
                        default=8)

    parser.add_argument('-w','--writeconfig',help='Load current Linode proxies configuration and write a fresh proxies.list config file', action='store_true')
    parser.add_argument('-W','--writelbconfig',help='Load current Linode proxies configuration and write a fresh HAProxy config to /etc/haproxy/haproxy.cfg', action='store_true')
    parser.add_argument('--restart',help='Restart the daemon',action='store_true')

    args = parser.parse_args()
    # print args

    rotator = ProxyRotator(cfg=args.conf,
                           test_mode = args.test,
                           rotate=args.rotate)

    if args.test:
        print 'Testing the daemon'
        rotator.test()
        sys.exit(0)

    if args.add != 0:
        print 'Adding new set of',args.num,'linode proxies ...'
        rotator.provision(count = int(args.num), add=True)
        sys.exit(0)

    if args.provision != 0:
        print 'Provisioning fresh set of',args.num,'linode proxies ...'
        rotator.provision(count = int(args.num))
        sys.exit(0)

    if args.create:
        print 'Creating new linode...'
        rotator.create(int(args.region))
        sys.exit(0)

    if args.drop:
        print 'Dropping current proxies ...'
        rotator.drop()
        sys.exit(0)

    if args.writeconfig:
        # Load current proxies config and write proxies.list file
        if rotator.config.vps_provider == 'linode':

            proxies_list = rotator.linode_cmd.list_instances()
            proxies = list()
            for item in proxies_list:
                proxies.append(",".join((str(item.ipv4[0]), str(item.region.id), str(item.id),str(0),str(0))))

            print >> open('proxies.list', 'w'), '\n'.join(proxies)


        elif rotator.config.vps_provider == 'aws':
            print >> open('proxies.list', 'w'), '\n'.join(rotator.aws_command.list_proxies())
        print 'Saved current proxy configuration to proxies.list'
        sys.exit(0)

    if args.writelbconfig:
        # Load current proxies config and write proxies.list file
        rotator.config.write_lb_config()
        print 'Wrote HAProxy configuration'
        sys.exit(0)


    if args.stop or args.restart:
        pidfile = 'rotator.pid'
        if os.path.isfile(pidfile):
            print 'Stopping proxy rotator daemon ...',
            # Signal the running daemon with SIGTERM
            try:
                os.kill(int(open(pidfile).read().strip()), signal.SIGTERM)
                print 'stopped.'
            except OSError, e:
                print e
                print 'Unable to stop, possibly daemon not running.'

        if args.restart:
            print 'Starting...'
            os.system('python rotate_proxies.py')

        sys.exit(1)

    rotator.run(daemon=(not args.nodaemon))

