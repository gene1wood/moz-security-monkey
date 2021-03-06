#     Copyright 2014 Netflix, Inc.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

from flask.ext.script import Manager, Command, Option
from security_monkey import app, db
from security_monkey.common.route53 import Route53Service
from gunicorn.app.base import Application

from flask.ext.migrate import Migrate, MigrateCommand

from moz_security_monkey.scheduler import run_change_reporter as sm_run_change_reporter
from moz_security_monkey.scheduler import find_changes as sm_find_changes
from moz_security_monkey.scheduler import audit_changes as sm_audit_changes
from moz_security_monkey.backup import backup_config_to_json as sm_backup_config_to_json
from moz_security_monkey.common.utils.utils import prep_accounts
from security_monkey.datastore import Account
from security_monkey.datastore import User

import csv

manager = Manager(app)
migrate = Migrate(app, db)
manager.add_command('db', MigrateCommand)

@manager.command
def drop_db():
    """ Drops the database. """
    db.drop_all()

@manager.command
def create_db():
    """ Drops the database. """
    db.create_all()

@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
def run_change_reporter(accounts):
    """ Runs Reporter """
    sm_run_change_reporter(accounts)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
def find_changes(accounts, monitors):
    """Runs watchers"""
    sm_find_changes(accounts, monitors)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
@manager.option('-r', '--send_report', dest='send_report', type=bool, default=False)
def audit_changes(accounts, monitors, send_report):
    """ Runs auditors """
    sm_audit_changes(accounts, monitors, send_report)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
@manager.option('-o', '--outputfolder', dest='outputfolder', type=unicode, default=u'backups')
def backup_config_to_json(accounts, monitors, outputfolder):
    """Saves the most current item revisions to a json file."""
    sm_backup_config_to_json(accounts, monitors, outputfolder)


@manager.command
def start_scheduler():
    """ starts the python scheduler to run the watchers and auditors"""
    from moz_security_monkey import scheduler
    scheduler.setup_scheduler()
    scheduler.scheduler.start()

@manager.option('-f', '--filename', dest='filename', type=unicode)
def add_accounts(filename):
    from security_monkey.common.utils.utils import add_account
    with open(filename, 'rb') as csvfile:
        csvreader = csv.reader(csvfile)
        for row in csvreader:
            number = row[0]
            name = row[1]
            role_name = row[2]
            res = add_account(number=number,
                              third_party=False,
                              name=name,
                              s3_name=None,
                              active=True,
                              notes=None,
                              role_name=role_name)
            if res:
                app.logger.info('Successfully added account {}'.format(name))
            else:
                app.logger.info('Account with id {} already exists'.format(number))

@manager.option('-b', '--bucket', type=unicode,
                default=u'infosec-internal-data')
@manager.option('--role-file', type=unicode,
                default=u'iam-roles/roles.json')
@manager.option('--alias-file', type=unicode,
                default=u'iam-roles/account-aliases.json')
@manager.option('-t', '--trusted-entity', type=unicode,
                default=u'arn:aws:iam::371522382791:root')
@manager.option('-r', '--role-type', type=unicode,
                default=u'InfosecSecurityAuditRole')
@manager.option('--third-party-file', type=unicode,
                default=u'iam-roles/third-party-aws-accounts.json')
def add_all_accounts(bucket, role_file, alias_file, trusted_entity, role_type, third_party_file):
    import boto3, json, botocore.exceptions
    from security_monkey.common.utils.utils import add_account

    # TODO : Convert this to boto instead of boto3
    # TODO : Describe json schema here
    client = boto3.client('s3')
    response = client.get_object(
        Bucket=bucket,
        Key=role_file)
    roles = json.load(response['Body'])

    response = client.get_object(
        Bucket=bucket,
        Key=alias_file)
    aliases = json.load(response['Body'])

    for role in [x for x in roles if
                 x['TrustedEntity'] == trusted_entity
                 and x['Type'] == role_type]:
        account_id = role['Arn'].split(':')[4]
        session = boto3.Session()
        client_sts = session.client('sts')
        try:
            response_sts = client_sts.assume_role(
                RoleArn=role['Arn'],
                RoleSessionName='fetch_aliases')
        except botocore.exceptions.ClientError:
            print('Unable to assume role {}'.format(role['Arn']))
            continue
        credentials = {
            'aws_access_key_id': response_sts['Credentials']['AccessKeyId'],
            'aws_secret_access_key': response_sts['Credentials'][
                'SecretAccessKey'],
            'aws_session_token': response_sts['Credentials']['SessionToken']}
        client_iam = boto3.client('iam', **credentials)
        response_iam = client_iam.list_account_aliases()
        if len(response_iam['AccountAliases']) == 1:
            alias = response_iam['AccountAliases'][0]
        elif account_id in aliases:
            alias = aliases[account_id]
        else:
            alias = account_id
        params = {
            'number': role['Arn'].split(':')[4],
            'third_party': False,
            'name': alias[:32],
            's3_name': u'',
            'active': True,
            'notes': alias,
            'role_name': role['Arn'].split(':')[5].split('/')[1]
        }
        # print(json.dumps(params))
        result = add_account(**params)
        if result:
            print('Successfully added account {}:{}'.format(
                params['name'], params['number']))
        else:
            print('Account with id {} already exists'.format(params['number']))

    response = client.get_object(
        Bucket=bucket,
        Key=third_party_file)
    third_parties = json.load(response['Body'])

    for account in third_parties:
        params = {
            'number': account,
            'third_party': True,
            'name': third_parties[account]['name'],
            's3_name': u'',
            'active': False,
            'notes': third_parties[account]['documentation'],
            'role_name': u''
        }
        result = add_account(**params)
        if result:
            print('Successfully added third party account {}:{}'.format(
                params['name'], params['number']))
        else:
            print('Third party account {} already exists'.format(params['name']))


@manager.option('-a', '--accounts', dest='account_numbers', type=unicode,
                default=u'all')
def remove_accounts(account_numbers):
    accounts = Account.query.filter(
        Account.third_party == False).filter(Account.active == True).all()
    if account_numbers == 'all':
        account_ids = [account.id for account in accounts]
    else:
        account_ids = [account.id for account in accounts
                       if account.number in account_numbers.split(',')]
    for account_id in account_ids:
        users = User.query.filter(User.accounts.any(Account.id == account_id)).all()
        for user in users:
            user.accounts = [account for account in user.accounts if not account.id == account_id]
            db.session.add(user)
        db.session.commit()

        account = Account.query.filter(Account.id == account_id).first()

        print("Deleting account {}".format(account.name))
        db.session.delete(account)
        db.session.commit()

        # query = Account.query.filter(Account.number == account)
        # if query.count():
        #     print("Deleting account {}".format(accounts))
        #     query.delete()

if __name__ == "__main__":
    manager.run()
