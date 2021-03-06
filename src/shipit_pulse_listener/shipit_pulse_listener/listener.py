# -*- coding: utf-8 -*-
import asyncio

import requests

from cli_common.log import get_logger
from cli_common.pulse import run_consumer
from cli_common.utils import retry
from shipit_pulse_listener import task_monitoring
from shipit_pulse_listener.hook import Hook
from shipit_pulse_listener.hook import PulseHook

logger = get_logger(__name__)


class HookPhabricator(Hook):
    '''
    Taskcluster hook handling the static analysis
    for Phabricator differentials
    '''
    latest_id = None

    def __init__(self, configuration):
        assert 'hookId' in configuration
        super().__init__(
            'project-releng',
            configuration['hookId'],
        )

        # Save phabricator connection data
        assert 'phabricator_url' in configuration
        assert 'phabricator_token' in configuration
        self.api_url = configuration['phabricator_url']
        self.api_token = configuration['phabricator_token']

        # Start by getting top id
        diffs, _ = self.request_phabricator(limit=1)
        assert len(diffs) == 1
        self.latest_id = diffs[0]['id']

    def request_phabricator(self, limit=20, order='newest'):
        '''
        Load raw differential objects from the api
        '''
        logger.info('Loading phabricator differentials', after=self.latest_id, limit=limit, order=order)
        url = '{}/differential.diff.search'.format(self.api_url)
        payload = {
            'api.token': self.api_token,
            'after': self.latest_id,
            'order': order,
            'limit': limit,
        }
        response = requests.post(url, payload)
        response.raise_for_status()
        data = response.json()
        assert data['error_code'] is None, \
            'Conduit error: {} - {}'.format(
                data['error_code'],
                data['error_info'],
            )

        return data['result']['data'], data['result']['cursor']['after']

    def list_differential(self):
        '''
        List new differential items using pagination
        using an iterator
        '''
        cursor = self.latest_id
        while cursor is not None:
            diffs, cursor = self.request_phabricator(order='oldest')
            if not diffs:
                break

            for diff in diffs:
                yield diff

            # Update the latest id
            if cursor:
                self.latest_id = cursor
            elif len(diffs) > 0:
                self.latest_id = diffs[-1]['id']

    async def build_consumer(self, *args, **kwargs):
        '''
        Query phabricator differentials regularly
        '''
        while True:

            # Get new differential ids
            for diff in self.list_differential():
                if diff['type'] != 'DIFF':
                    logger.info('Skipping differential', id=diff['id'], type=diff['type'])
                    continue

                # Create new task
                await self.create_task({
                    'ANALYSIS_SOURCE': 'phabricator',
                    'ANALYSIS_ID': diff['phid']
                })

            # Sleep a bit before trying new diffs
            await asyncio.sleep(60)


class HookStaticAnalysis(PulseHook):
    '''
    Taskcluster hook handling the static analysis
    for MozReview
    '''
    def __init__(self, configuration):
        assert 'hookId' in configuration
        super().__init__(
            'project-releng',
            configuration['hookId'],
            'exchange/mozreview/',
            'mozreview.commits.published',
        )

    def parse(self, body):
        '''
        Extract revisions from payload
        '''
        if 'payload' not in body:
            raise Exception('Missing payload in body')
        payload = body['payload']

        # Filter on repo url
        repository_url = payload.get('repository_url')
        if not repository_url:
            raise Exception('Missing repository url in payload')
        if repository_url != 'https://reviewboard-hg.mozilla.org/gecko':
            logger.info('Skipping this message, invalid repository url', url=repository_url)  # noqa
            return

        # Extract commits
        envs = [
            {
                'ANALYSIS_SOURCE': 'mozreview',
                'ANALYSIS_ID': c['review_request_id'],
                'MOZREVIEW_REVISION': c['rev'],
                'MOZREVIEW_DIFFSET': c['diffset_revision'],
            }

            for c in payload.get('commits', [])
        ]
        logger.info('Received new commits', revs=[e['MOZREVIEW_REVISION'] for e in envs])
        return envs


class HookCodeCoverage(PulseHook):
    '''
    Taskcluster hook handling the code coverage
    '''
    def __init__(self, configuration):
        assert 'hookId' in configuration
        self.triggered_groups = set()
        super().__init__(
            'project-releng',
            configuration['hookId'],
            'exchange/taskcluster-queue/v1/task-group-resolved',
            '*.*.gecko-level-3._'
        )

    def is_coverage_task(self, task):
        return any(task['task']['metadata']['name'].startswith(s) for s in ['build-linux64-ccov', 'build-win64-ccov'])

    def is_mozilla_central_task(self, task):
        if 'GECKO_HEAD_REPOSITORY' not in task['task']['payload']['env']:
            logger.warn('Received groupResolved notification for a task without GECKO_HEAD_REPOSITORY', task_id=task['status']['taskId'])
            return False

        repo = task['task']['payload']['env']['GECKO_HEAD_REPOSITORY']

        if repo != 'https://hg.mozilla.org/mozilla-central':
            logger.warn('Received groupResolved notification for a non-mozilla-central coverage task', repo=repo)
            return False

        return True

    def get_build_task_in_group(self, group_id):
        if group_id in self.triggered_groups:
            logger.info('Received duplicated groupResolved notification', group=group_id)
            return None

        def maybe_trigger(tasks):
            for task in tasks:
                if self.is_coverage_task(task):
                    self.triggered_groups.add(group_id)
                    return task

            return None

        list_url = 'https://queue.taskcluster.net/v1/task-group/{}/list'.format(group_id)

        def retrieve_coverage_task():
            r = requests.get(list_url, params={
                'limit': 200
            })
            r.raise_for_status()
            reply = r.json()
            task = maybe_trigger(reply['tasks'])

            while task is None and 'continuationToken' in reply:
                r = requests.get(list_url, params={
                    'limit': 200,
                    'continuationToken': reply['continuationToken']
                })
                r.raise_for_status()
                reply = r.json()
                task = maybe_trigger(reply['tasks'])

            return task

        try:
            return retry(retrieve_coverage_task)
        except requests.exceptions.HTTPError:
            return None

    def parse(self, body):
        '''
        Extract revisions from payload
        '''
        taskGroupId = body['taskGroupId']

        build_task = self.get_build_task_in_group(taskGroupId)
        if build_task is None:
            return None

        if not self.is_mozilla_central_task(build_task):
            return None

        logger.info('Received groupResolved notification for coverage builds', revision=build_task['task']['payload']['env']['GECKO_HEAD_REV'], group=taskGroupId)  # noqa

        return [{
            'REVISION': build_task['task']['payload']['env']['GECKO_HEAD_REV'],
        }]


class PulseListener(object):
    '''
    Listen to pulse messages and trigger new tasks
    '''
    def __init__(self,
                 pulse_user,
                 pulse_password,
                 hooks_configuration,
                 taskcluster_client_id=None,
                 taskcluster_access_token=None,
                 ):

        self.pulse_user = pulse_user
        self.pulse_password = pulse_password
        self.hooks_configuration = hooks_configuration
        self.taskcluster_client_id = taskcluster_client_id
        self.taskcluster_access_token = taskcluster_access_token

        task_monitoring.connect_taskcluster(
            self.taskcluster_client_id,
            self.taskcluster_access_token,
        )

    def run(self):

        # Build hooks for each conf
        hooks = [
            self.build_hook(conf)
            for conf in self.hooks_configuration
        ]
        if not hooks:
            raise Exception('No hooks created')

        # Run hooks pulse listeners together
        # but only use hooks with active definitions
        consumers = [
            hook.build_consumer(self.pulse_user, self.pulse_password)
            for hook in hooks
            if hook.connect_taskcluster(
                self.taskcluster_client_id,
                self.taskcluster_access_token,
            )
        ]

        # Add monitoring process
        consumers.append(task_monitoring.run())

        # Run all consumers together
        run_consumer(asyncio.gather(*consumers))

    def build_hook(self, conf):
        '''
        Build a new hook instance according to configuration
        '''
        assert isinstance(conf, dict)
        assert 'type' in conf
        classes = {
            'static-analysis-mozreview': HookStaticAnalysis,
            'static-analysis-phabricator': HookPhabricator,
            'code-coverage': HookCodeCoverage,
        }
        hook_class = classes.get(conf['type'])
        if hook_class is None:
            raise Exception('Unsupported hook {}'.format(conf['type']))

        return hook_class(conf)
