from funcx.sdk.file import GlobusInstanceList
from funcx.serialize import FuncXSerializer


class Batch:
    """Utility class for creating batch submission in funcX"""

    def __init__(self, task_group_id=None):
        """
        Parameters
        ==========

        task_group_id : str
            UUID indicating the task group that this batch belongs to
        """
        self.tasks = []
        self.fx_serializer = FuncXSerializer()
        self.task_group_id = task_group_id

    def add(
        self, *args, remote_data=None, endpoint_id=None, function_id=None, **kwargs
    ):
        """Add an function invocation to a batch submission

        Parameters
        ----------
        *args : Any
            Args as specified by the function signature
        remote_data : GlobusInstanceList object
            remote data path. Optional
        endpoint_id : uuid str
            Endpoint UUID string. Required
        function_id : uuid str
            Function UUID string. Required
        asynchronous : bool
            Whether or not to run the function asynchronously

        Returns
        -------
        None
        """
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"
        assert function_id is not None, "function_id key-word argument must be set"
        if remote_data:
            assert isinstance(
                remote_data, GlobusInstanceList
            ), "Please use GlobusInstanceList to define your remote data"

        ser_args = self.fx_serializer.serialize(args)
        ser_kwargs = self.fx_serializer.serialize(kwargs)
        payload = self.fx_serializer.pack_buffers([ser_args, ser_kwargs])

        data_url = remote_data.generate_url() if remote_data else None
        # data_url covers the recursive attribute
        recursive = False if remote_data else False

        data = {
            "endpoint": endpoint_id,
            "function": function_id,
            "payload": payload,
            "data_url": data_url,
            "recursive": recursive,
        }

        self.tasks.append(data)

    def prepare(self):
        """Prepare the payloads to be post to web service in a batch

        Parameters
        ----------

        Returns
        -------
        payloads in dictionary, Dict[str, list]
        """
        data = {"task_group_id": self.task_group_id, "tasks": []}

        for task in self.tasks:
            new_task = (
                task["function"],
                task["endpoint"],
                task["payload"],
                task["data_url"],
                task["recursive"],
            )
            data["tasks"].append(new_task)

        return data
