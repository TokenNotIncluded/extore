"""A local demo; replace with your merchant-installed fulfillment handler."""

from extore.sdk import Result, Task, run


def fulfill(task: Task):
    task.progress(30, "正在准备欢迎函")
    return Result.success(
        f"你好，{task.params['name']}！\n\n这是你的 Extore 欢迎函。\n任务编号：{task.id}",
        message="欢迎函已准备好",
    )


if __name__ == "__main__":
    run(fulfill)
