import logging
import json
import asyncio
from dotenv import load_dotenv
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import openai, deepgram, silero, turn_detector
from livekit import rtc
from exam_db_driver import ExamDBDriver

load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("voice-agent")

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["db"] = ExamDBDriver()  # Initialize ExamDBDriver here

async def entrypoint(ctx: JobContext):
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are an oral exam instructor. Your role is to:"
            "1. Ask questions from the exam one at a time"
            "2. Listen to the student's response, dig deeper once if needed"
            "3. Move to the next question after receiving the response"
            "4. Do not provide answers or hints"
            "5. End the exam with a completion message"
            "Do not ask questions until you receive the exam data."
        ),
    )

    logger.info(f"Connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    class ExamState:
        def __init__(self):
            self.exam = None
            self.current_question_idx = 0
            self.questions_asked = 0
            self.data_received = False
            self.exam_completed = False

    exam_state = ExamState()
    db_driver = ctx.proc.userdata["db"]

    # Define the agent early so we can use it in functions
    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=deepgram.TTS(),
        turn_detector=turn_detector.EOUModel(),
        min_endpointing_delay=0.5,
        max_endpointing_delay=5.0,
        chat_ctx=initial_ctx,
    )

    async def ask_next_question():
        if not exam_state.data_received:
            logger.warning("Attempted to ask next question but no exam data received")
            return
            
        if exam_state.exam is None:
            logger.error("Exam data was invalid")
            await agent.say("Exam data was invalid. Please try again.", allow_interruptions=False)
            return
            
        if exam_state.exam_completed:
            logger.info("Exam already completed")
            return
            
        if exam_state.current_question_idx < len(exam_state.exam.questions):
            question = exam_state.exam.questions[exam_state.current_question_idx].text
            logger.info(f"Asking question {exam_state.current_question_idx + 1}: {question[:30]}...")
            
            exam_state.questions_asked += 1
            await agent.say(f"Question {exam_state.current_question_idx + 1}: {question}", allow_interruptions=True)
            exam_state.current_question_idx += 1
        else:
            logger.info("All questions completed, ending exam")
            exam_state.exam_completed = True
            await agent.say(
                "Thank you for completing the exam. This concludes our session. Good luck with your results!",
                allow_interruptions=False
            )

    async def handle_data_received(data: rtc.DataPacket):
        logger.info(f"Data received handler triggered")
        if data.data:
            try:
                message = data.data.decode("utf-8")
                logger.info(f"Decoded message: {message}")
                message_json = json.loads(message)

                logger.info(f"Received data: {message_json}")
                exam_state.data_received = True

                if message_json.get("type") == "QUESTIONS":
                    data_obj = message_json.get("data", {})
                    exam_id = data_obj.get("examId")
                    
                    if not exam_id:
                        logger.error("No examId found in message")
                        return

                    logger.info(f"Looking up exam with ID: {exam_id}")
                    # Fetch exam from database using instance method
                    exam_state.exam = db_driver.get_exam_by_id(exam_id)

                    if exam_state.exam:
                        logger.info(f"Successfully loaded exam: {exam_state.exam.name} with {len(exam_state.exam.questions)} questions")

                        welcome_msg = (
                            f"Welcome to the Coral AI Exam Platform! I'm your AI instructor. "
                            f"We'll now begin the {exam_state.exam.name} exam, which contains "
                            f"{len(exam_state.exam.questions)} questions. The exam duration is "
                            f"{exam_state.exam.duration} minutes and has a {exam_state.exam.difficulty} difficulty level. "
                            f"Let's start with the first question."
                        )
                        await agent.say(welcome_msg, allow_interruptions=False)

                        # Add a slight delay before first question
                        await asyncio.sleep(2)
                        await ask_next_question()
                    else:
                        logger.error(f"Failed to load exam with ID: {exam_id}")
                        await agent.say("Sorry, I couldn't load the exam. Please try again.", allow_interruptions=False)

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON data: {e}")
            except Exception as e:
                logger.error(f"Error processing data: {e}", exc_info=True)

    # Register this handler BEFORE connecting participants
    ctx.room.on("data_received", lambda data: asyncio.create_task(handle_data_received(data)))
    
    # Wait for participant after setting up event handlers
    participant = await ctx.wait_for_participant()
    logger.info(f"Starting voice assistant for participant {participant.identity}")
    logger.info(f"Agent's own identity: {ctx.room.local_participant.identity}")

    # Custom handler for when participant speaks and finishes speaking
    async def on_user_speech_committed():
        logger.info("User speech committed, moving to next question")
        # Add delay to give a more natural conversation flow
        await asyncio.sleep(1.5)
        if not exam_state.exam_completed:
            await ask_next_question()

    agent.on("user_speech_committed", lambda _: asyncio.create_task(on_user_speech_committed()))
    
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        logger.info(f"Participant connected: {participant.identity}")

    # Start the agent
    agent.start(ctx.room, participant)

    # Periodic check to see if we've received data
    check_interval = 5  # seconds
    max_checks = 12  # 60 seconds total
    checks = 0
    
    while checks < max_checks and not exam_state.data_received:
        await asyncio.sleep(check_interval)
        checks += 1
        logger.info(f"Data check {checks}/{max_checks}: Data received: {exam_state.data_received}")
        
        if checks == 3 and not exam_state.data_received:  # After 15 seconds
            await agent.say("Waiting for exam data. Please ensure it has been sent to the room.", allow_interruptions=False)

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )
