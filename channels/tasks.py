from celery import shared_task
from .channel import Group


@shared_task
def send_messages_async(group_name, message):
    """
    Sends it to all provided group names async
    """
    group = Group(group_name)
    group.send(message)
