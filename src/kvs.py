''' SpyGlass implementation that sends OpenCV frames to AWS Kinesis Video Streams. '''

import re
import os
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.credentials import RefreshableCredentials

from .spyglass import SpyGlass
from .timepiece import AtSchedule, local_now

def _is_refreshable(credentials):
    return isinstance(credentials, RefreshableCredentials)

def _format_time(datetime_):
    return datetime_.strftime('%Y-%m-%dT%H:%M:%SZ')

class KVSSpyGlass(SpyGlass):

    ''' Sends OpenCV frames to Kinesis Video Streams.

    KVSSpyGlass can be used to create programatically a video stream and send
    it to AWS Kinesis Video Streams service.

    When initializing the KVSSpyGlass() instance, you should provide the AWS
    credentials that will be used to stream the video in your AWS account.
    The `CredentialsHandler` subclasses implement different ways of passing the
    credentials to the underlying Kinesis Video Stream Producer. In most of the
    cases, `FileCredentialsHandler` with the default arguments should work well,
    as long as your AWS user or the assume IAM Role have a policy to write put
    data in KVS.

    You can configure the frame width, height and fps auto-detection as described
    in the SpyGlass class documentation.

    :param stream_region: The AWS region of the Kinesis Video Stream
    :param stream_name: The name of the Kinesis Video Stream
    :param credentials_handler: The credentials handler
    :param *args: Positional arguments to be passed to SpyGlass initializer.
    :param *kwargs: Keyword arguments to be passed to SpyGlass initializer.
    '''

    def __init__(self,
        stream_region: str,
        stream_name: str,
        credentials_handler: 'KVSCredentialsHandler',
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.stream_region = stream_region
        self.stream_name = stream_name
        self.credentials_handler = credentials_handler
        self._check_gst_plugin('kvssink')

    def _get_pipeline(self, fps, width, height):
        credentials_config_str = self.credentials_handler.plugin_config()
        kvs_config_str = ' '.join([
            'storage-size=512',
            f'stream-name="{self.stream_name}"',
            f'aws-region="{self.stream_region}"',
            f'framerate={fps}',
            credentials_config_str
        ])
        pipeline = ' ! '.join([
            'appsrc',
            'videoconvert',
            f'video/x-raw,format=I420,width={width},height={height},framerate={fps}/1',
            'x264enc bframes=0 key-int-max=45 bitrate=500',
            'video/x-h264,stream-format=avc,alignment=au,profile=baseline',
            f'kvssink {kvs_config_str}'
        ])
        pipeline_safe = self.credentials_handler.plugin_config_mask(pipeline)
        self.logger.info(f'GStreamer pipeline definition:\n{pipeline_safe}')
        return pipeline

    def _put_frame(self, frame, timestamp, show_timestamp):
        result = super()._put_frame(frame, timestamp, show_timestamp)
        self.credentials_handler.check_refresh()
        return result


class KVSCredentialsHandler:
    ''' Provide AWS credentials to Kinesis Video Stream Producer library.

    If no static credentials are provided in the init method, the Credentials
    Handler will figure out the AWS credentials from the boto3 configuration.

    Use of static credentials are recommended at most for testing purposes. If you
    instantiate this class with custom credentials in the init method arguments, or
    calling the init method from a workstation where you've configured an AWS user
    credentials, you will get static credentials. If the caller code assumed an IAM
    role, you will use dynamic credentials.

    When using dynamic credentials, you are expected to call the `check_refresh`
    method peridocally to control the expiration of the dynamic credentials. Ideally
    you would call this method each time when you want send a new frame to Kinesis
    Video Streams Producer. If the credentials are not expired, this method should add
    almost no overhead.

    :param aws_access_key_id: If you want to use custom static credentials, specify your
        AWS access key ID here. This method is not recommended for production builds.
    :param aws_secret_access_key: If you want to use custom static credentials, specify your
        AWS secret access key here. This method is not recommended for production builds.
    :param parent_logger: Connect the logger of this class to a parent logger.
    '''

    REFRESH_BEFORE_EXPIRATION = datetime.timedelta(seconds=2 * 60)
    ''' Refresh credentials so many seconds before expiration. '''

    def __init__(
        self,
        aws_access_key_id: str=None,
        aws_secret_access_key: str=None,
        parent_logger: logging.Logger=None
    ):
        self.logger = (
            logging.getLogger(self.__class__.__name__) if parent_logger is None else
            parent_logger.getChild(self.__class__.__name__)
        )
        if aws_access_key_id or aws_secret_access_key:
            self.session = boto3.Session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_access_key_id
            )
        else:
            self.session = boto3.Session()

        self.caller_arn = self.session.client('sts').get_caller_identity()['Arn']
        self.credentials = self.session.get_credentials()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.schedule = AtSchedule(at=None, callback=self._refresh, executor=self.executor)
        self._refresh()

    def _refresh(self):
        # pylint: disable=protected-access
        # since botocore credentials do not give access to expiry date, 
        # we've to use protected members
        if _is_refreshable(self.credentials):
            self.logger.info(f'Refreshing credentials using {self.caller_arn}')
            # This will refresh the credentials if neeeded
            advisory = self.credentials.refresh_needed(self.credentials._advisory_refresh_timeout)
            mandatory = self.credentials.refresh_needed(self.credentials._mandatory_refresh_timeout)
            self.logger.info(f'Refresh needed: advisory={advisory}, mandatory={mandatory}')
            self.logger.info(f'Forcing refresh credentials with method '
                             f'{self.credentials._refresh_using}')
            self.credentials._protected_refresh(is_mandatory=True)
            frozen_credentials = self.credentials.get_frozen_credentials()
            expiry_time = self.credentials._expiry_time
            self.logger.info('Got credentials: '
                             f'access_key={frozen_credentials.access_key[:5]}..., '
                             f'secret_key={frozen_credentials.secret_key[:5]}..., '
                             f'token={frozen_credentials.token[:5]}..., '
                             f'expiry={_format_time(expiry_time)}'
            )

            # Schedule next refresh when we're in the mandatory refresh timeout
            # of the underlying credentials
            #
            #                          token expiry_time+
            #                                           |
            #  advisory_refresh_timeout+                |
            #                          |<---- 15m ----->|
            #                          |                |
            #      mandatory_refresh_timeout+           |
            # now                      |    |<-- 10m -->|
            #  v                       v    v           v
            #  -----------------------------------------|
            #                                 ^
            #                   update_timeout+
            #
            next_update = expiry_time - self.REFRESH_BEFORE_EXPIRATION
            credentials = frozen_credentials
            self.logger.info(f'Next update: {_format_time(next_update)}')
            now = local_now()
            if next_update < now:
                msg = ('Next update time is in the past! '
                    f'current_time={_format_time(now)}, '
                    f'next_update={_format_time(next_update)}, '
                    f'expiry={_format_time(expiry_time)}'
                )
                self.logger.warning(msg)
        else:
            self.logger.info('Credentials are static')
            next_update = None
            credentials = self.credentials
        self.save_credentials(credentials, next_update)
        self.schedule.at = next_update

    def check_refresh(self):
        ''' Call this method periodically to refresh credentials. '''
        self.schedule.tick()

    def save_credentials(
        self,
        credentials: 'botocore.credentials.Credentials',
        next_update: datetime.datetime
    ):
        ''' Saves the credentials for Kinesis Video Stream Producer component.

        This method should be implemented in subclasses.

        :param credentials: The credentials to be saved.
        :param next_update: When the next credentials update is expected.
        '''

    def plugin_config(self) -> str: # pylint: disable=no-self-use
        ''' Returns a string that should be included in the kvssing plugin config.'''
        return ''

    def plugin_config_mask(self, plugin_config) -> str: # pylint: disable=no-self-use
        ''' Masks credentials for printing in logs. '''
        return plugin_config


class KVSInlineCredentialsHandler(KVSCredentialsHandler):
    ''' Provides AWS credentials inline in the kvssink plugin config.

    This credentials handler can be used only with static credentials as there is
    no way to refresh the credentials once they were passed to KVS Producer.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if _is_refreshable(self.credentials):
            raise RuntimeError(
                'InlineCredentialsHandler must not be used with refreshable '
                'credentials as they will not be refreshed once passed to '
                'kvssink plugin configuration.'
            )

    def plugin_config(self):
        return ' '.join([
            f'access-key="{self.credentials.access_key}"',
            f'secret-key="{self.credentials.secret_key}"',
        ])

    def plugin_config_mask(self, plugin_config):
        ''' Masks credentials for printing in logs. '''
        res = plugin_config
        res = re.sub(r'secret-key="([^"]*)"', 'secret-key="*****"', res)
        res = re.sub(r'access-key="([^"]*)"', 'access-key="*****"', res)
        return res


class KVSEnvironmentCredentialsHandler(KVSCredentialsHandler):
    ''' Saves AWS credentials in environment variables.

    Experience shows that the kvssink plugin does not check periodically the
    environment variables for updated dynamic credentials, so there are chances
    that your stream will stop once the original dynamic credentials expiry.

    For this reason, it is recommened that you use this credentials handler
    only with static credentials.
    '''

    def save_credentials(self, credentials, next_update):
        if _is_refreshable(self.credentials):
            credentials = credentials.get_frozen_credentials()
            os.environ['AWS_SESSION_TOKEN'] = credentials.token
        os.environ['AWS_ACCESS_KEY_ID'] = credentials.access_key
        os.environ['AWS_SECRET_ACCESS_KEY'] = credentials.secret_key


class KVSFileCredentialsHandler(KVSCredentialsHandler):
    ''' Saves AWS variables in a text file compatible with `credential-path`
    parameter of kvssink.

    Using this credentials handler establishes the most robust communication
    between the handler and the Kinesis Video Stream Producer. The credentials
    are written into a text file with a predefined format. This handler can be
    used both with static and dynamic credentials: the handler will ensure that
    the refreshed credentials are updated also in the text file, before the
    declared file expiration.

    :param credentials_path: The path of the credentials file.
    '''

    FILE_REFRESH_GRACE_PERIOD = datetime.timedelta(seconds=60)
    '''  Give some time (60s) to boto3 credentials handler to effectively
    refresh the credentials, and declare the expiration of the KVS
    credentials file after the waiting this time, KVS will find the
    new tokens in the file. '''

    def __init__(self, credentials_path: str='/tmp/credentials.txt', **kwargs):
        self.credentials_path = credentials_path
        super().__init__(**kwargs)

    def save_credentials(self, credentials, next_update):
        if next_update is not None:
            file_expire = next_update + self.FILE_REFRESH_GRACE_PERIOD
            file_expire_str = _format_time(file_expire)
            self.logger.info(f'Credentials file expiration: {file_expire_str}')

            credentials_str = '\t'.join([
                'CREDENTIALS',
                credentials.access_key,
                file_expire_str,
                credentials.secret_key,
                credentials.token
            ])
            self.logger.info(
                f'Updated {self.credentials_path}:\n' + '\t'.join([
                    'CREDENTIALS',
                    f'{credentials.access_key[:5]}...',
                    file_expire_str,
                    f'{credentials.secret_key[:5]}...',
                    f'{credentials.token[:5]}...',
                ])
            )
        else:
            credentials_str = '\t'.join([
                'CREDENTIALS',
                credentials.access_key,
                credentials.secret_key
            ])
            self.logger.info(
                f'Updated {self.credentials_path}:\n' + '\t'.join([
                    'CREDENTIALS',
                    f'{credentials.access_key[:5]}...',
                    f'{credentials.secret_key[:5]}...',
                ])
            )

        with open(self.credentials_path, 'w', encoding='utf-8') as credentials_file:
            credentials_file.write(credentials_str)

    def plugin_config(self):
        return f'credential-path="{self.credentials_path}"'
