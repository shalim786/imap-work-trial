"""Create a fresh isolated Thunderbird profile pointing only at the local sandbox."""
import argparse
import json
from pathlib import Path
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=1143)
    parser.add_argument('--profile', type=Path, default=Path('var/thunderbird-profile'))
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error('port must be between 1 and 65535')
    profile = args.profile.resolve()
    profile.mkdir(parents=True, exist_ok=False)
    (profile / 'ImapMail/127.0.0.1').mkdir(parents=True)
    (profile / 'Mail/Local Folders').mkdir(parents=True)
    prefs = {
        'mail.account.account1.identities': 'id1', 'mail.account.account1.server': 'server1',
        'mail.account.account2.server': 'server2', 'mail.accountmanager.accounts': 'account1,account2',
        'mail.accountmanager.defaultaccount': 'account1', 'mail.accountmanager.localfoldersserver': 'server2',
        'mail.identity.id1.fullName': 'Synthetic IMAP Acceptance',
        'mail.identity.id1.useremail': 'candidate@imap.test', 'mail.identity.id1.valid': True,
        'mail.server.server1.authMethod': 3, 'mail.server.server1.hostname': '127.0.0.1',
        'mail.server.server1.userName': 'candidate@imap.test', 'mail.server.server1.type': 'imap',
        'mail.server.server1.port': args.port, 'mail.server.server1.socketType': 0,
        'mail.server.server1.name': 'Local synthetic AgentMail',
        'mail.server.server1.directory': str(profile / 'ImapMail/127.0.0.1'),
        'mail.server.server1.directory-rel': '[ProfD]ImapMail/127.0.0.1',
        'mail.server.server1.login_at_startup': True, 'mail.server.server1.check_new_mail': True,
        'mail.server.server1.offline_download': False, 'mail.server.server1.autosync_offline_stores': False,
        'mail.server.server1.max_cached_connections': 2,
        'mail.server.server2.hostname': 'Local Folders', 'mail.server.server2.userName': 'nobody',
        'mail.server.server2.type': 'none', 'mail.server.server2.name': 'Local Folders',
        'mail.server.server2.directory': str(profile / 'Mail/Local Folders'),
        'mail.server.server2.directory-rel': '[ProfD]Mail/Local Folders',
        'mail.shell.checkDefaultClient': False, 'mail.rights.version': 1,
        'mailnews.start_page.enabled': False, 'mailnews.message_display.disable_remote_image': True,
        'mailnews.database.global.indexer.enabled': False,
        'datareporting.policy.dataSubmissionEnabled': False, 'toolkit.telemetry.enabled': False,
        'mail.provider.enabled': False,
    }
    (profile / 'user.js').write_text(''.join(f'user_pref({json.dumps(key)}, {json.dumps(value)});\n' for key, value in prefs.items()))
    executable = '/Applications/Thunderbird.app/Contents/MacOS/thunderbird'
    print(f'Created isolated profile: {profile}')
    print('Start the local sandbox/server, then launch:')
    print(f'{shlex.quote(executable)} --new-instance --profile {shlex.quote(str(profile))}')
    print('Enter synthetic password test_agentmail_key when prompted. No SMTP account is configured.')


if __name__ == '__main__':
    main()
