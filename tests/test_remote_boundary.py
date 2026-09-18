import inspect
import unittest

from simulator.openhands.remote_tools import RemoteFileEditorTool


class RemoteToolBoundaryTests(unittest.TestCase):
    """Guard the factory boundary without opening SSH or instantiating a tool."""

    def test_file_editor_factory_uses_remote_executor_directly(self):
        source = inspect.getsource(RemoteFileEditorTool.create)

        self.assertIn("RemoteFileEditorExecutor", source)
        self.assertIn("executor=exe", source)
        self.assertNotIn("FileEditorTool.create", source)
        self.assertNotIn("super().create", source)


if __name__ == "__main__":
    unittest.main()
