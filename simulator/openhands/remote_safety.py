"""Stop the SDK turn on uncertain transport/executor failures."""
import json
from pathlib import Path
from openhands.sdk.tool import ToolExecutor


class StopOnUncertainExecution(ToolExecutor):
    def __init__(self, executor, state_dir):
        self.executor = executor
        self.marker = Path(state_dir)/'fatal.json'

    def __call__(self, action, conversation=None):
        if self.marker.exists():
            if conversation is not None:
                conversation.pause()
            raise RuntimeError('uncertain execution retained; automatic continuation denied')
        try:
            return self.executor(action, conversation)
        except Exception as error:
            self.marker.parent.mkdir(parents=True,exist_ok=True)
            temporary = self.marker.with_suffix('.tmp')
            temporary.write_text(json.dumps({'error_type':type(error).__name__,
                                            'reason':str(error), 'action':action.model_dump(mode='json')}))
            temporary.replace(self.marker)
            if conversation is not None:
                conversation.pause()
            raise

    def close(self):
        self.executor.close()
