import os
import unittest
from unittest.mock import patch
from agentmail_imap.config import Config

class AWSConfigTests(unittest.TestCase):
    def test_cloud_defaults_and_infra_names(self):
        with patch.dict(os.environ, {'IMAP_DB_HOST':'database.example','IMAP_DB_PASSWORD':'private'},clear=True):
            config = Config.from_env('/missing')
        self.assertEqual((config.host,config.port,config.storage_backend,config.tls_mode),('0.0.0.0',1143,'postgres','terminated'))
        self.assertEqual(config.database_host,'database.example')
        self.assertEqual(config.database_password,'private')
        self.assertEqual(config.database_sslmode,'verify-full')

    def test_direct_config_preserves_protocol_test_defaults(self):
        self.assertEqual(Config().storage_backend,'sqlite')
        self.assertEqual(Config().host,'127.0.0.1')
