#!/usr/bin/python3

import accounts # must be first
import log
import time
import sys
from vkbot import VkBot, CONF_START
from vkapi import VkApi
import re
import check_friend
from calc import evalExpression
import random
import config
import signal
from server import MessageServer
import threading
from args import args
import stats

from prepare import login, password
from cppbot import CppBot  # should go after prepare import

def isBotMessage(msg, regex=re.compile(r'^\(.+\).')):
    return regex.match(msg.strip())

bot = CppBot()

noans = open(accounts.getFile('noans.txt'), encoding='utf-8').read().splitlines()
smiles = open(accounts.getFile('smiles.txt'), encoding='utf-8').read().splitlines()
random.shuffle(noans)

class BanManager:
    def __init__(self, filename):
        self.filename = filename
        banign = open(filename).read().split()
        self.banned = set(map(int, banign))

    def write(self):
        s = list(map(str, sorted(self.banned)))
        with open(self.filename, 'w') as f:
            f.write('\n'.join(s))

    def ban(self, pid):
        if pid in self.banned:
            return False
        self.banned.add(pid)
        self.write()
        return True

    def unban(self, pid):
        if pid not in self.banned:
            return False
        self.banned.discard(pid)
        self.write()
        return True

def timeto(name, interval, d={}):
    if interval >= 0 and time.time() > d.get(name, 0) + interval:
        d[name] = time.time()
        return True
    return False

def renderSmile(s, regex=re.compile(r'&#(\d+);')):
    return regex.sub(lambda x:chr(int(x.group(1))), s)


last_reply_lower = set()
_cmd_re = re.compile(r'\\[a-zA-Z]+')
# conf_id == -1: comment
# conf_id == -2: flat
def getBotReply(uid, message, conf_id, method='', onsend_actions=None):
    if message is None:
        return None

    message = message.replace('\u0401', '\u0415').replace('\u0451', '\u0435')  # yo
    message = message.replace('\u0490', '\u0413').replace('\u0491', '\u0433')  # g
    message = message.replace('\u0404', '\u042d').replace('\u0454', '\u044d')  # e
    message = message.replace('\u0406', '\u0418').replace('\u0456', '\u0438')  # i
    message = message.replace('\u0407', '\u0418').replace('\u0457', '\u0438')  # i
    message = message.replace("`", "'")

    if conf_id == 0:
        answer = bot.interact('user {} {}'.format(uid, message))
    elif conf_id > 0:
        answer = bot.interact('conf {} {}'.format(uid, message))
    elif conf_id in (-1, -2):
        answer = bot.interact('{} {}'.format('comm' if conf_id == -1 else 'flat 0', message))
        bl = (answer == '$blacklisted')
        return bl

    if answer == '$noans':
        if message.upper() == message.lower() and '?' not in message:
            answer = random.choice(smiles)
        else:
            answer = noans[0]
            next_ans = random.randint(1, len(noans) - 1)
            noans[0], noans[next_ans] = noans[next_ans], noans[0]
    elif answer == '$blacklisted':
        answer = ''

    if message == message.lower() and message != message.upper() or message.upper() == message.lower() and uid in last_reply_lower:
        last_reply_lower.add(uid)
        answer = answer.lower()
    else:
        last_reply_lower.discard(uid)
    console_message = ''

    if '{' in answer:
        answer, gender = applyGender(answer, uid)
        console_message += ' (' + gender + ')'

    if '\\' in answer:
        res = _cmd_re.sub(lambda m:preprocessReply(m.group(0)[1:], uid, onsend_actions), answer)
        console_message += ' (' + answer + ')'
        answer = res

    if method:
        console_message += ' (' + method + ')'
    text_msg = '({}) {} : {}{}'.format(vk.printableSender({'user_id':uid, 'chat_id':conf_id}, False), message, renderSmile(answer), console_message)
    html_msg = '({}) {} : {}{}'.format(vk.printableSender({'user_id':uid, 'chat_id':conf_id}, True), message, renderSmile(answer).replace('&', '&amp;'), console_message)
    log.info((text_msg, html_msg))
    return answer

def processCommand(cmd, *p):
    if cmd == 'reload':
        return reloadHandler()

    elif cmd == 'banned':
        if banign.banned:
            result = sorted(banign.banned)
            result = [vk.printableName(j, user_fmt='[id{id}|{name}]') for j in result]
            return '\n'.join(result)
        else:
            return 'No one banned!'

    elif cmd == 'ban':
        if not p:
            return 'Not enough parameters'
        user = vk.getUserId(p[-1])
        if user is None:
            return 'No such user'
        if user == vk.admin:
            return 'Cannot ban admin!'
        return vk.printableName(user, user_fmt='[id{id}|{name}]') + ' banned' if banign.ban(user) else 'Already banned'

    elif cmd == 'unban':
        if not p:
            return 'Not enough parameters'
        user = p[-1]
        user = vk.getUserId(user)
        return vk.printableName(user, user_fmt='[id{id}|{name}]') + ' unbanned' if banign.unban(user) else 'Not banned'

    elif cmd == 'ignore':
        users = vk.getUserId(p)
        if not users:
            return 'No such users'
        if vk.admin in users:
            return 'Cannot ignore admin!'
        noaddUsers(users, reason='\\ignore command')
        vk.users.load(users)
        return 'Ignored ' +  ', '.join(vk.printableName(i, user_fmt='[id{id}|{name}]') for i in users)

    elif cmd == 'unignore':
        users = vk.getUserId(p)
        if not users:
            return 'No such users'
        if vk.admin in users:
            return 'Cannot ignore admin!'
        noaddUsers(users, True)
        vk.users.load(users)
        return 'Unignored ' +  ', '.join(vk.printableName(i, user_fmt='[id{id}|{name}]') for i in users)

    elif cmd == 'leave':
        if not p:
            return 'Not enough parameters'
        if not p[-1].isdigit():
            return 'Invalid conf id'
        cid = int(p[-1])
        if vk.leaveConf(cid):
            return 'Ok'
        else:
            return 'Fail'

    else:
        return 'Unknown command'


bot_users = {}

# returns (text, mode)
# mode=0: default, mode=1: no delay, mode=2: friendship request
def reply(message):
    if vk.getSender(message) in banign.banned:
        vk.banned_list.append(vk.getSender(message))
        return None
    if vk.getSender(message) < 0:
        return None
    if vk.getSender(message) in check_friend.noadd or message['user_id'] in check_friend.noadd:
        return (None, 0)
    if 'deactivated' in vk.users[message['user_id']] or vk.users[message['user_id']]['blacklisted'] or vk.users[message['user_id']]['blacklisted_by_me']:
        return (None, 0)

    if 'body' not in message:
        message['body'] = ''

    onsend_actions = []

    if 'id' not in message:  # friendship request
        return (getBotReply(message['user_id'], message['message'], 0, 'friendship request', onsend_actions), 2)

    if isBotMessage(message['body']):
        vk.logSender('(%sender%) {} - ignored (bot message)'.format(message['body']), message)
        if 'chat_id' in message:
            bot_users[message['user_id']] = bot_users.get(message['user_id'], 0) + 1
            if bot_users[message['user_id']] >= 3:
                log.info('Too many bot messages')
                log.write('conf', str(message['user_id']) + ' ' + str(message['chat_id']) + ' (bot messages)')
                vk.leaveConf(message['chat_id'])
        return ('', 0)
    elif message['user_id'] in bot_users:
        del bot_users[message['user_id']]

    message['body'] = preprocessMessage(message)

    if message['body']:
        user_msg = vk.last_message.byUser(message['user_id'])
        if message['body'].startswith('\\') and len(message['body']) > 1:
            cmd = message['body'][1:].split()
            if cmd and message['user_id'] == vk.admin:
                return (processCommand(*cmd), 1)

        if message['body'] == user_msg.get('text') and message['body'] != '..':
            user_msg['count'] = user_msg.get('count', 0) + 1  # this modifies the cache entry too
            if user_msg['count'] >= 5:
                noaddUsers([message['user_id']], reason='flood')
            else:
                vk.logSender('(%sender%) {} - ignored (repeated)'.format(message['body']), message)
            return ('', 0)

        if 'reply' in user_msg and message['body'].upper() == user_msg['reply'].upper():
            vk.logSender('(%sender%) {} - ignored (my reply)'.format(message['body']), message)
            user_msg['text'] = user_msg['reply']  # this modifies the cache entry too
            #user_msg['count'] = 1  # do we need it?
            return ('', 0)

        t = evalExpression(message['body'])
        if t:
            if getBotReply(None, message['body'], -2):
                return ('', 0)
            vk.logSender('(%sender%) {} = {} (calculated)'.format(message['body'], t), message)
            log.write('calc', '{}: "{}" = {}'.format(message['user_id'], message['body'], t))
            return (t, 0)
        tbody = message['body'].replace('<br>', '')
        if tbody.upper() == tbody and sum(i.isalpha() for i in tbody) > 1:
            vk.logSender('(%sender%) {} - ignored (caps)'.format(message['body']), message)
            return ('', 0)

    reply = getBotReply(message['user_id'], message['body'] , message.get('chat_id', 0), message.get('_method', ''), onsend_actions)
    message['_onsend_actions'] = onsend_actions
    return (reply, 0)


def preprocessMessage(message, user=None):
    if user is not None and message.get('user_id') != user:
        return None

    if 'action' in message:
        if message['action'] == 'chat_invite_user' and message['action_mid'] == vk.self_id:
            vk.deleteFriend(message['user_id'])
        return None

    result = message['body']
    att = []
    for a in message.get('attachments', []):
        if a['type'] == 'audio':
            att.append(a['audio']['title'])
        elif a['type'] == 'video':
            att.append(a['video']['title'])
        elif a['type'] == 'wall':
            att.append(a['wall']['text'])
            if not a['wall']['text'] and 'copy_history' in a['wall']:
                att[-1] = a['wall']['copy_history'][0]['text']
        elif a['type'] == 'doc':
            att.append(a['doc']['title'])
        elif a['type'] == 'gift':
            att.append('vkgift')
        elif a['type'] == 'link':
            att.append(a['link']['description'])
        elif a['type'] == 'market':
            att.append(a['market']['description'])
        elif a['type'] == 'sticker':
            return None
        elif a['type'] == 'photo':
            result += ' ..'
    for a in att:
        result += ' [' + a.lower() + ']'
    result = result.replace('vkgift', 'Vkgift')

    for fwd in message.get('fwd_messages', []):
        if len(message['fwd_messages']) == 1 and fwd.get('user_id') == vk.self_id and result:
            continue
        r = preprocessMessage(fwd, message.get('user_id'))
        if r is None:
            return None
        result  += ' {' + str(r) + '}'

    if user is None and 'attachments' not in message and not result:
        return None
    return result.strip()


def preprocessReply(s, uid, onsend_actions):
    if s == 'myname':
        return vk.users[uid]['first_name']
    if s == 'mylastname':
        return vk.users[uid]['last_name']
    if s == 'curtime':
        return time.strftime("%H:%M", time.localtime())
    if s == 'likeava':
        onsend_actions.append(lambda: vk.likeAva(uid))
        return ''
    if s == 'gosp':
        onsend_actions.append(lambda: vk.setRelation(uid))
        return ''
    if s == 'phone':
        return vk.phone
    if s == 'age':
        return config.get('birthday.age')
    if s == 'name':
        return vk.name[0]
    if s == 'lastname':
        return vk.name[1]
    if s == 'bf':
        if vk.bf:
            return 'https://vk.com/id' + str(vk.bf['id'])
        else:
            return ''
    if s == 'bfname':
        if vk.bf:
            return vk.bf['first_name']
        else:
            return ''
    if s == 'bflastname':
        if vk.bf:
            return vk.bf['last_name']
        else:
            return ''
    log.error('Unknown variable: ' + s)


# 1: female, 2: male
def applyGender(msg, uid, male_re=re.compile(r'\{m([^\{\}]*)\}'), female_re=re.compile(r'\{f([^\{\}]*)\}')):
    gender = ['male', 'female', 'male'][vk.users[uid]['sex']]
    if gender == 'female':
        msg = male_re.sub('', msg)
        msg = female_re.sub('\\1', msg)
    else:
        msg = female_re.sub('', msg)
        msg = male_re.sub('\\1', msg)
    return msg, gender

def testFriend(uid, need_reason=False):
    fr = vk.users[uid]
    if fr is None:
        return False
    return check_friend.isGood(fr, need_reason)

def noaddUsers(users, remove=False, reason=None):
    users = set(users)
    if not users:
        return 0
    with vk.api.api_lock:
        if remove:
            prev_len = len(check_friend.noadd)
            check_friend.noadd -= users
            check_friend.writeNoadd()
            return prev_len - len(check_friend.noadd)
        else:
            users -= check_friend.noadd
            users.discard(vk.admin)
            if not users:
                return 0
            text_msg = 'Deleting ' + ', '.join([vk.printableSender({'user_id':i}, False) for i in users]) + (' ({})'.format(reason) if reason else '')
            html_msg = 'Deleting ' + ', '.join([vk.printableSender({'user_id':i}, True) for i in users]) + (' ({})'.format(reason) if reason else '')
            log.info((text_msg, html_msg))
            check_friend.appendNoadd(users)
            vk.deleteFriend(users)
            return len(users)


def reloadHandler(*p):
    bot.interact('reld')
    vk.initSelf()
    log.info('Reloaded!')
    return 'Reloaded!'

def onExit(*p):
    log.info('Received SIGTERM')
    loop_thread.join(60)
    vk.waitAllThreads()
    log.info('Bye')
    sys.exit()
signal.signal(signal.SIGTERM, onExit)


vk = VkBot(login, password)
vk.admin = config.get('inf.admin', 'i')
vk.bad_conf_title = lambda s: getBotReply(None, s, -2)
log.info('My id: ' + str(vk.self_id))
banign = BanManager(accounts.getFile('banned.txt'))
if args['whitelist']:
    vk.whitelist = [vk.getUserId(i) for i in args['whitelist'].split(',')]
    log.info('Whitelist: ' +', '.join(map(lambda x:vk.printableName(x, user_fmt='{name}'), vk.whitelist)))


addfriends_interval = config.get('inf.addfriends_interval', 'i')
includeread_interval = config.get('inf.includeread_interval', 'i')
setonline_interval = config.get('inf.setonline_interval', 'i')
unfollow_interval = config.get('inf.unfollow_interval', 'i')
filtercomments_interval = config.get('inf.filtercomments_interval', 'i')
stats_interval = config.get('inf.stats_interval', 'i')

def ignoreHandler(user):
    user = vk.getUserId(user)
    if not user:
        return 'Invalid user'
    if noaddUsers([user], reason='external command'):
        return 'Ignored ' + vk.printableName(user, user_fmt='{name}')
    else:
        return vk.printableName(user, user_fmt='{name}') + ' already ignored'
def unignoreHandler(user):
    user = vk.getUserId(user)
    if not user:
        return 'Invalid user'
    if noaddUsers([user], True):
        return 'Unignored ' + vk.printableName(user, user_fmt='{name}')
    else:
        return vk.printableName(user, user_fmt='{name}') + ' is not ignored'

def banHandler(user):
    user = vk.getUserId(user)
    if not user:
        return 'Invalid user'
    return ('Banned {}' if banign.ban(user) else '{} already banned').format(vk.printableName(user, user_fmt='{name}'))

def unbanHandler(user):
    user = vk.getUserId(user)
    if not user:
        return 'Invalid user'
    return ('Unbanned {}' if banign.unban(user) else '{} is not banned').format(vk.printableName(user, user_fmt='{name}'))

def isignoredHandler(user):
    user = vk.getUserId(user)
    if user is None or user > CONF_START:
        return 'Invalid user'
    r = testFriend(user, True)
    if r is None:
        return 'Good'
    return r

def leaveHandler(conf):
    conf = vk.getUserId(conf)
    if conf > CONF_START:
        conf -= CONF_START
    if vk.leaveConf(conf):
        return 'Ok'
    else:
        return 'Fail'

def banlistHandler(conf):
    if banign.banned:
        result = sorted(banign.banned)
        result = [vk.printableName(j, user_fmt='<a href="https://vk.com/id{id}">{name}</a><br>') for j in result]
        return '\n'.join(result)
    else:
        return 'No one banned!'

if config.get('inf.server_port', 'i'):
    srv = MessageServer()
    srv.addHandler('reply', lambda x:bot.interact('flat ' + x, False))
    srv.addHandler('stem', lambda x:bot.interact('stem ' + x, False))
    srv.addHandler('ignore', ignoreHandler)
    srv.addHandler('unignore', unignoreHandler)
    srv.addHandler('ban', banHandler)
    srv.addHandler('unban', unbanHandler)
    srv.addHandler('reload', reloadHandler)
    srv.addHandler('isignored', isignoredHandler)
    srv.addHandler('leave', leaveHandler)
    srv.addHandler('banlist', banlistHandler)
    srv.listen()
    log.info('Running TCP server on port ' + config.get('inf.server_port'))

check_friend.writeNoadd()
stats.update('started', time.time())
reply_all = timeto('includeread', includeread_interval)
def main_loop():
    global reply_all, dialogs
    try:
        if timeto('setonline', setonline_interval):
            vk.setOnline()
        if timeto('filtercomments', filtercomments_interval):
            noaddUsers(vk.filterComments(lambda s:getBotReply(None, s, -1)), reason='bad comment')
            stats.update('blacklisted', vk.blacklistedCount())
        if timeto('unfollow', unfollow_interval):
            noaddUsers(vk.unfollow(banign.banned), reason='deleted me')
        if timeto('addfriends', addfriends_interval):
            vk.addFriends(reply, testFriend)
        if includeread_interval >= 0:
            vk.replyAll(reply, reply_all)
        else:
            time.sleep(1)
        reply_all = vk.api.captcha_error
        vk.api.captcha_error = False
        if timeto('includeread', includeread_interval):
            reply_all = True
        if timeto('stats', stats_interval):
            count, dialogs, confs = vk.lastDialogs()
            vk.loadUsers(dialogs, lambda x:x[1])
            dialogs = [[uid, vk.printableName(uid, '{name}', conf_fmt='Conf "%s"' % confs.get(uid)), cnt] for uid, cnt in dialogs]
            stats.update('dialogs', count)
            stats.update('dialogs_list', dialogs)

    except Exception as e:
        log.error('global {}: {}'.format(e.__class__.__name__, str(e)), True)
        reply_all = True
        time.sleep(2)

while True:
    loop_thread = threading.Thread(target=main_loop)
    loop_thread.start()
    loop_thread.join()
