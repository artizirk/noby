import unittest
import io
import noby


class DockerfileTestCase(unittest.TestCase):
    def test_load(self):
        dockerfile = io.StringIO("""
        FROM scratch
        ENV bla=bla
        # ENV not=not
        HOST echo
        COPY bla bla
        RUN bla
        """)
        dockerfile.open = lambda: dockerfile
        parser = noby.DockerfileParser(dockerfile)
        self.assertEqual(parser.env, {"bla":"bla"})


if __name__ == '__main__':
    unittest.main()
