from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import io
import json
import logging
import os
import time

import requests
from future.moves.urllib.parse import quote_plus
from typing import Callable, Union
from typing import Text, List, Optional, Dict, Any

from rasa_core import utils
from rasa_core.actions.action import ACTION_LISTEN_NAME
from rasa_core.channels import InputChannel
from rasa_core.channels import UserMessage
from rasa_core.dispatcher import Dispatcher
from rasa_core.domain import Domain, TemplateDomain
from rasa_core.events import BotUttered
from rasa_core.events import Event
from rasa_core.trackers import DialogueStateTracker

logger = logging.getLogger(__name__)


class RasaCoreClient(object):
    """Connects to a running Rasa Core server.

    Used to retrieve information about models and conversations."""

    def __init__(self, host, token):
        self.host = host
        self.token = token

    def status(self):
        url = "{}/version?token={}".format(self.host, self.token)
        result = requests.get(url)
        return result.json()

    def all_clients(self):
        url = "{}/conversations?token={}".format(self.host, self.token)
        result = requests.get(url)
        return result.json()

    def retrieve_tracker(self, sender_id, domain,
                         only_events_after_latest_restart=False,
                         include_events=True,
                         until=None):
        tracker_json = self.retrieve_tracker_json(
                sender_id, only_events_after_latest_restart,
                include_events, until)

        tracker = DialogueStateTracker.from_dict(sender_id,
                                                 tracker_json.get("events", []),
                                                 domain)
        return tracker

    def retrieve_tracker_json(self, sender_id,
                              use_history=True,
                              include_events=True,
                              until=None):
        url = ("{}/conversations/{}/tracker?token={}"
               "&ignore_restarts={}"
               "&events={}").format(
                self.host, sender_id, self.token,
                use_history,
                include_events)
        if until:
            url += "&until={}".format(until)
        result = requests.get(url)
        return result.json()

    def append_events_to_tracker(self, sender_id, events):
        # type: (Text, List[Event]) -> None
        url = "{}/conversations/{}/tracker/events?token={}".format(
                self.host, sender_id, self.token)
        result = requests.post(url, json=[event.as_dict() for event in events])
        return result.json()

    def parse(self, message, sender_id):
        # type: (UserMessage, Text) -> Optional[Dict[Text, Any]]
        """Send a parse request to a rasa core server."""

        url = "{}/conversations/{}/parse?token={}".format(
                self.host, sender_id, quote_plus(self.token))
        data = json.dumps({"query": message}, ensure_ascii=False)
        response = requests.post(url, data=data.encode("utf-8"),
                                 headers={
                                     'Content-type': 'text/plain; '
                                                     'charset=utf-8'})
        if response.status_code == 200:
            return response.json()
        else:
            logger.warn("Got a bad response from rasa core :( Status: {} "
                        "Response: {}".format(response.status_code,
                                              response.text))
            return None

    def upload_model(self, model_dir, max_retries=1):
        url = "{}/load?token={}".format(self.host, quote_plus(self.token))
        logger.debug("Uploading model to rasa core server.")

        model_zip = utils.zip_folder(model_dir)

        response = None
        while max_retries > 0:
            with io.open(model_zip, "rb") as f:
                response = requests.post(url, files={"model": f})
            max_retries -= 1
            if response.status_code == 200:
                logger.debug("Finished uploading")
                return response.json()
            else:
                time.sleep(2)
                max_retries -= 1

        logger.warn("Got a bad response from rasa core while uploading "
                    "the model (Status: {} "
                    "Response: {}".format(response.status_code,
                                          response.text))
        return None

    def continue_core(self, action_name, events, sender_id):
        # type: (Text, List[Event], Text) -> Optional[Dict[Text, Any]]
        """Send a continue request to rasa core to get next action
        prediction."""

        url = "{}/conversations/{}/continue?token={}".format(
                self.host, sender_id, quote_plus(self.token))
        dumped_events = []
        for e in events:
            dumped_events.append(e.as_dict())
        data = json.dumps(
                {"executed_action": action_name, "events": dumped_events},
                ensure_ascii=False)
        response = requests.post(url, data=data.encode('utf-8'),
                                 headers={
                                     'Content-type': 'text/plain; '
                                                     'charset=utf-8'})

        if response.status_code == 200:
            return response.json()
        else:
            logger.warn("Got a bad response from rasa core :( Status: {} "
                        "Response: {}".format(response.status_code,
                                              response.text))
            return None


class RemoteAgent(object):
    def __init__(
            self,
            domain,  # type: Union[Text, Domain]
            core_client
    ):
        self.domain = domain
        self.core_client = core_client

    def handle_channel(
            self,
            input_channel,  # type: InputChannel
            message_preprocessor=None  # type: Optional[Callable[[Text], Text]]
    ):
        # type: (...) -> None
        """Handle incoming messages from the input channel using remote core."""

        def message_handler(message):
            if message_preprocessor is not None:
                message.text = message_preprocessor(message.text)
            self.process_message(message)

        logger.info("Starting sync listening on input channel")
        input_channel.start_sync_listening(message_handler)

    def process_message(self, message):
        # type: (UserMessage) -> None
        """Process a message using a remote rasa core instance."""

        # message = UserMessage(text, , sender_id)
        response = self.core_client.parse(message.text, message.sender_id)

        while response and response.get("next_action") != ACTION_LISTEN_NAME:
            dispatcher = Dispatcher(message.sender_id,
                                    message.output_channel,
                                    self.domain)
            action_name = response.get("next_action")
            tracker = self.core_client.retrieve_tracker(message.sender_id,
                                                        self.domain)

            if action_name is not None:
                action = self.domain.action_for_name(action_name)
                # events and return values are used to update
                # the tracker state after an action has been taken
                try:
                    action_events = action.run(dispatcher, tracker, self.domain)
                except Exception as e:
                    logger.error(
                            "Encountered an exception while running action "
                            "'{}'. Bot will continue, but the actions "
                            "events are lost. Make sure to fix the "
                            "exception in your custom code."
                            "".format(action.name()))
                    logger.error(e, exc_info=True)
                    action_events = []
                events = []
                for m in dispatcher.latest_bot_messages:
                    events.append(BotUttered(text=m.text, data=m.data))

                events.extend(action_events)
                response = self.core_client.continue_core(action_name,
                                                          events,
                                                          message.sender_id)
            else:
                logger.error("Rasa Core did not return an action. Response: "
                             "{}".format(response))
                response = None
        logger.info("Done processing message")

    @classmethod
    def load(cls,
             path,  # type: Text
             core_host,  # type: Text
             auth_token,  # type: Optional[Text]
             action_factory=None  # type: Optional[Text]
             ):
        # type: (...) -> RemoteAgent

        domain = TemplateDomain.load(os.path.join(path, "domain.yml"),
                                     action_factory)

        core_client = RasaCoreClient(core_host, auth_token)
        core_client.upload_model(path, max_retries=5)

        return RemoteAgent(domain, core_client)