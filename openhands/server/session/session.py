import asyncio
import json
import time
from copy import deepcopy

import socketio

from openhands.controller.agent import Agent
from openhands.core.config import AppConfig
from openhands.core.const.guide_url import TROUBLESHOOTING_URL
from openhands.core.logger import openhands_logger as logger
from openhands.core.schema import AgentState
from openhands.events.action import MessageAction, NullAction
from openhands.events.event import Event, EventSource
from openhands.events.observation import (
    AgentStateChangedObservation,
    CmdOutputObservation,
    NullObservation,
)
from openhands.events.observation.error import ErrorObservation
from openhands.events.serialization import event_from_dict, event_to_dict
from openhands.events.stream import EventStreamSubscriber
from openhands.llm.llm import LLM
from openhands.server.session.agent_session import AgentSession
from openhands.server.session.conversation_init_data import ConversationInitData
from openhands.storage.files import FileStore
from openhands.storage.locations import get_conversation_init_data_filename
from openhands.utils.async_utils import call_sync_from_async

ROOM_KEY = 'room:{sid}'


class Session:
    sid: str
    sio: socketio.AsyncServer | None
    last_active_ts: int = 0
    is_alive: bool = True
    agent_session: AgentSession
    loop: asyncio.AbstractEventLoop
    config: AppConfig
    file_store: FileStore

    def __init__(
        self,
        sid: str,
        config: AppConfig,
        file_store: FileStore,
        sio: socketio.AsyncServer | None,
    ):
        self.sid = sid
        self.sio = sio
        self.last_active_ts = int(time.time())
        self.file_store = file_store
        self.agent_session = AgentSession(
            sid, file_store, status_callback=self.queue_status_message
        )
        self.agent_session.event_stream.subscribe(
            EventStreamSubscriber.SERVER, self.on_event, self.sid
        )
        # Copying this means that when we update variables they are not applied to the shared global configuration!
        self.config = deepcopy(config)
        self.loop = asyncio.get_event_loop()

    def close(self):
        self.is_alive = False
        self.agent_session.close()

    async def _restore_init_data(self, sid: str) -> ConversationInitData:
        # FIXME: we should not store/restore this data once we have server-side
        # LLM configs. Should be done by 1/1/2025
        json_str = await call_sync_from_async(
            self.file_store.read, get_conversation_init_data_filename(sid)
        )
        data = json.loads(json_str)
        return ConversationInitData(**data)

    async def _save_init_data(self, sid: str, init_data: ConversationInitData):
        # FIXME: we should not store/restore this data once we have server-side
        # LLM configs. Should be done by 1/1/2025
        json_str = json.dumps(init_data.__dict__)
        await call_sync_from_async(
            self.file_store.write, get_conversation_init_data_filename(sid), json_str
        )

    async def initialize_agent(
        self, conversation_init_data: ConversationInitData | None = None
    ):
        self.agent_session.event_stream.add_event(
            AgentStateChangedObservation('', AgentState.LOADING),
            EventSource.ENVIRONMENT,
        )
        if conversation_init_data is None:
            try:
                conversation_init_data = await self._restore_init_data(self.sid)
            except FileNotFoundError:
                logger.error(f'User settings not found for session {self.sid}')
                raise RuntimeError('User settings not found')

        agent_cls = conversation_init_data.agent or self.config.default_agent
        self.config.security.confirmation_mode = (
            self.config.security.confirmation_mode
            if conversation_init_data.confirmation_mode is None
            else conversation_init_data.confirmation_mode
        )
        self.config.security.security_analyzer = (
            conversation_init_data.security_analyzer
            or self.config.security.security_analyzer
        )
        max_iterations = (
            conversation_init_data.max_iterations or self.config.max_iterations
        )
        # override default LLM config

        default_llm_config = self.config.get_llm_config()
        default_llm_config.model = (
            conversation_init_data.llm_model or default_llm_config.model
        )
        default_llm_config.api_key = (
            conversation_init_data.llm_api_key or default_llm_config.api_key
        )
        default_llm_config.base_url = (
            conversation_init_data.llm_base_url or default_llm_config.base_url
        )
        await self._save_init_data(self.sid, conversation_init_data)

        # TODO: override other LLM config & agent config groups (#2075)

        llm = LLM(config=self.config.get_llm_config_from_agent(agent_cls))
        agent_config = self.config.get_agent_config(agent_cls)
        agent = Agent.get_cls(agent_cls)(llm, agent_config)

        try:
            await self.agent_session.start(
                runtime_name=self.config.runtime,
                config=self.config,
                agent=agent,
                max_iterations=max_iterations,
                max_budget_per_task=self.config.max_budget_per_task,
                agent_to_llm_config=self.config.get_agent_to_llm_config_map(),
                agent_configs=self.config.get_agent_configs(),
                github_token=conversation_init_data.github_token,
                selected_repository=conversation_init_data.selected_repository,
            )
        except Exception as e:
            logger.exception(f'Error creating controller: {e}')
            await self.send_error(
                f'Error creating controller. Please check Docker is running and visit `{TROUBLESHOOTING_URL}` for more debugging information..'
            )
            return

    async def on_event(self, event: Event):
        """Callback function for events that mainly come from the agent.
        Event is the base class for any agent action and observation.

        Args:
            event: The agent event (Observation or Action).
        """
        if isinstance(event, NullAction):
            return
        if isinstance(event, NullObservation):
            return
        if event.source == EventSource.AGENT:
            await self.send(event_to_dict(event))
        elif event.source == EventSource.USER:
            await self.send(event_to_dict(event))
        # NOTE: ipython observations are not sent here currently
        elif event.source == EventSource.ENVIRONMENT and isinstance(
            event, (CmdOutputObservation, AgentStateChangedObservation)
        ):
            # feedback from the environment to agent actions is understood as agent events by the UI
            event_dict = event_to_dict(event)
            event_dict['source'] = EventSource.AGENT
            await self.send(event_dict)
        elif isinstance(event, ErrorObservation):
            # send error events as agent events to the UI
            event_dict = event_to_dict(event)
            event_dict['source'] = EventSource.AGENT
            await self.send(event_dict)

    async def dispatch(self, data: dict):
        event = event_from_dict(data.copy())
        # This checks if the model supports images
        if isinstance(event, MessageAction) and event.image_urls:
            controller = self.agent_session.controller
            if controller:
                if controller.agent.llm.config.disable_vision:
                    await self.send_error(
                        'Support for images is disabled for this model, try without an image.'
                    )
                    return
                if not controller.agent.llm.vision_is_active():
                    await self.send_error(
                        'Model does not support image upload, change to a different model or try without an image.'
                    )
                    return
        self.agent_session.event_stream.add_event(event, EventSource.USER)

    async def send(self, data: dict[str, object]):
        if asyncio.get_running_loop() != self.loop:
            self.loop.create_task(self._send(data))
            return
        await self._send(data)

    async def _send(self, data: dict[str, object]) -> bool:
        try:
            if not self.is_alive:
                return False
            if self.sio:
                await self.sio.emit('oh_event', data, to=ROOM_KEY.format(sid=self.sid))
            await asyncio.sleep(0.001)  # This flushes the data to the client
            self.last_active_ts = int(time.time())
            return True
        except RuntimeError:
            logger.error('Error sending', stack_info=True, exc_info=True)
            self.is_alive = False
            return False

    async def send_error(self, message: str):
        """Sends an error message to the client."""
        await self.send({'error': True, 'message': message})

    async def _send_status_message(self, msg_type: str, id: str, message: str):
        """Sends a status message to the client."""
        if msg_type == 'error':
            await self.agent_session.stop_agent_loop_for_error()

        await self.send(
            {'status_update': True, 'type': msg_type, 'id': id, 'message': message}
        )

    def queue_status_message(self, msg_type: str, id: str, message: str):
        """Queues a status message to be sent asynchronously."""
        asyncio.run_coroutine_threadsafe(
            self._send_status_message(msg_type, id, message), self.loop
        )