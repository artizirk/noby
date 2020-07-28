import unittest
import tempfile
import os
import noby


class ImageStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix='noby-test-', dir='.')

    def tearDown(self):
        os.removedirs(self.runtime)

    def test_runtime_supports_xattrs(self):
        val = b'test-value'
        attr = 'user.test-attr'
        os.setxattr(self.runtime, attr, val)
        self.assertEqual(os.getxattr(self.runtime, attr), val)


if __name__ == '__main__':
    unittest.main()
