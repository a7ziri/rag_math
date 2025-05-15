import asyncio
import os
import json
import traceback
import re
from typing import cast, List, Dict, Any, Optional, Union, Callable,Tuple
from dataclasses import dataclass
import lancedb
import logging
from ocr import MathOCR

import fire  # type: ignore
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
    User,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import Database
from provider import  LLMProvider
from pydantic import BaseModel, Field 
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
import matplotlib.pyplot as plt
from PIL import Image  

logging.basicConfig(level=logging.INFO)

ChatMessage = Dict[str, Any]
ChatMessages = List[ChatMessage]



class Step_calc(BaseModel):
    explanation: Optional[str] = Field(None, description="Четкое объяснение шага")
    calculation: Optional[str] = Field(None, description="Математические операции и их результат")
    verification: Optional[str] = Field(None, description="Как проверить этот шаг")
    final_answer: Optional[str] = Field(None, description="Итоговый ответ уравнения")
   

@dataclass
class BotConfig:
    token: str
    timezone: str = "Europe/Moscow"
    output_chunk_size: int = 3500


def _crop_content(content: str) -> str:
    if isinstance(content, str):
        return content.replace("\n", " ")[:40]
    return IMAGE_PLACEHOLDER


def _split_message(text: str, output_chunk_size: int) -> List[str]:
    if len(text) <= output_chunk_size:
        return [text]

    chunks: List[str] = []
    paragraphs = text.split("\n\n")
    for paragraph in paragraphs:
        if chunks and len(chunks[-1]) + len(paragraph) + 2 <= output_chunk_size:
            chunks[-1] += '\n\n' + paragraph
        else:
            chunks.append(paragraph)

    final_chunks: List[str] = []
    for chunk in chunks:
        if len(chunk) <= output_chunk_size:
            final_chunks.append(chunk)
            continue
        parts = [chunk[i : i + output_chunk_size] for i in range(0, len(chunk), output_chunk_size)]
        final_chunks.extend(parts)

    return final_chunks
    



async def _reply(message: Message, text: str, **kwargs: Any) -> Union[Message, bool]:
    try:
        return await message.reply(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception:
        try:
            return await message.reply(text, parse_mode=ParseMode.HTML, **kwargs)
        except Exception:
            return await message.reply(text, parse_mode=None, **kwargs)


async def _edit_text(message: Message, text: str, **kwargs: Any) -> Union[Message, bool]:
    try:
        return await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception:
        try:
            return await message.edit_text(text, parse_mode=ParseMode.HTML, **kwargs)
        except Exception:
            return await message.edit_text(text, parse_mode=None, **kwargs)



class LlmBot:
    def __init__(
        self,
        db_path: str,
        db_vector_path:str,
        providers_config_path: str,
        bot_config_path: str,
        subject_path:str,
    ):
        logging.info("Инициализация бота...")
        assert os.path.exists(bot_config_path)
        with open(bot_config_path) as r:
            self.config = BotConfig(**json.load(r))


        self.ocr = MathOCR()
        
        
        self.providers: Dict[str, LLMProvider] = dict()
        with open(providers_config_path, encoding='utf-8') as r:
            providers_config = json.load(r)
            for provider_name, config in providers_config.items():
                self.providers[provider_name] = LLMProvider(provider_name=provider_name, **config)

        self.subject = dict()
        assert os.path.exists(subject_path)
        with open(subject_path, encoding='utf-8') as r:
            self.subject  = json.load(r)


        self.db = Database(db_path)


        self.vectordb = lancedb.connect(db_vector_path)

        # self.document_loader = DocumentLoader()

        self.subject_kb = InlineKeyboardBuilder()
        for subject_name, identifier in self.subject.items():
            self.subject_kb.row(InlineKeyboardButton(text=subject_name, callback_data=f"set_subject:{subject_name}"))
        self.subject_kb.adjust(2)

        self.likes_kb = InlineKeyboardBuilder()
        self.likes_kb.add(InlineKeyboardButton(text="👍", callback_data="feedback:like"))
        self.likes_kb.add(InlineKeyboardButton(text="👎", callback_data="feedback:dislike"))
        


        self.bot = Bot(token=self.config.token, default=DefaultBotProperties(parse_mode=None))
        self.bot_info: Optional[User] = None

        self.dp = Dispatcher()
        commands: List[Tuple[str, Callable[..., Any]]] = [
            ("start", self.start),
            ("help", self.start),
            ("set_subject", self.set_subject),
            ("history", self.history),
            ("reset_subject", self.reset_subject),
            ("get_subject", self.get_subject),
            ("reset_history", self.reset_history),
            ("solve", self.handle_equation)
        ]
        for command, func in commands:
            self.dp.message.register(func, Command(command))
        self.dp.message.register(self.wrong_command, Command(re.compile(r"\S+")))
        self.dp.message.register(self.generate)

        callbacks: List[Tuple[str, Callable[..., Any]]] = [
            ("feedback:", self.save_feedback_handler),
            ("set_subject:", self.set_subject_button_handler)
        ]
        for start, func in callbacks:
            self.dp.callback_query.register(func, F.data.startswith(start))
        self.dp.callback_query.register(self.confirm_equation_handler, F.data == "confirm_equation")
        self.dp.callback_query.register(self.reject_equation_handler, F.data == "reject_equation")


        logging.info("Бот успешно инициализирован.")

    async def start(self, message: Message) -> None:
        assert message.from_user
        chat_id = message.chat.id

        # Create a conversation ID for the chat
        self.db.create_conv_id(chat_id)
        user_id = message.from_user.id

        # Send a welcome message to the user
        await message.reply("Привет, я  помогу ответить на твои  вопросы связанные  с математикой")

        # Send the command menu
        await self.send_command_menu(message.chat.id)

    async def wrong_command(self, message: Message) -> None:
        chat_id = message.chat.id
        assert message.from_user
        is_chat = chat_id != message.from_user.id
        if not is_chat:
            await message.reply("Такой команды у бота нет. Если вы не пытались ввести команду, уберите '/' из начала сообщения.")


    async def pre_checkout_handler(self, pre_checkout_query: PreCheckoutQuery) -> None:
            try:
                await self.bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
            except Exception as e:
                await self.bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message=str(e))


    async def reset_history(self, message: Message) -> None:
        chat_id = message.chat.id
        self.db.create_conv_id(chat_id)
        await message.reply('История сброщена')

    async def history(self, message: Message) -> None:
        chat_id = message.chat.id
        assert message.from_user
        is_chat = chat_id != message.from_user.id
        conv_id = self.db.get_current_conv_id(chat_id)
        history = self.db.fetch_conversation(conv_id)
        message_text = 'Истории не найдено'
        if history:
            message_text = f'История:{history}'
        await message.reply(message_text)


    async def start_polling(self) -> None:
        self.bot_info = await self.bot.get_me()
        await self.dp.start_polling(self.bot)


    async def _save_chat_message(self, message: Message) -> None:
        chat_id = message.chat.id
        assert message.from_user
        user_id = message.from_user.id
        user_name = self._get_user_name(message.from_user)
        content = await self._build_content(message)
        if content is not None:
            conv_id = self.db.get_current_conv_id(chat_id)
            self.db.save_user_message(content, conv_id=conv_id, user_id=user_id, user_name=user_name)

    @staticmethod
    def _format_chat(messages: ChatMessages) -> ChatMessages:
        for m in messages:
            content = m["content"]
            role = m["role"]
            if role == "user" and content is None:
                continue
            if role == "user" and isinstance(content, str) and m["user_name"]:
                m["content"] = "Из чата пишет {}: {}".format(m["user_name"], content)
        return messages
    async def set_subject(self, message: Message) -> None:
        try:
            await message.reply("Выберите предмет:", reply_markup=self.subject_kb.as_markup())
        except Exception as e:
            logging.error(f"Ошибка при выполнении команды /set_subject: {str(e)}")

    async def set_subject_button_handler(self, callback: CallbackQuery) -> None:
        try:
            assert callback.message
            assert callback.data
            chat_id = callback.message.chat.id
            subject_identifier = callback.data.split(":")[1]  # Измените индекс, если используется другой разделитель

            self.db.set_current_subject(chat_id, subject_identifier)
            self.db.create_conv_id(chat_id)
            await callback.message.edit_text(f"Выбранный предмет: {subject_identifier}")
        except Exception as e:
            logging.error(f"Ошибка в обработчике кнопки: {str(e)}")
            await callback.answer("Произошла ошибка при обработке вашего запроса.")



    async def reset_subject(self, message: Message) -> None:
        chat_id = message.chat.id
        # Reset the current subject in the database
        self.db.set_current_subject(chat_id, None)
        await message.reply("Выбор предмета сброшен.")

    async def get_subject(self, message: Message) -> None:
        chat_id = message.chat.id
        subject = self.db.get_current_subject(chat_id)
        
        # Проверка на None
        if subject is None or "subject" not in subject or subject["subject"] is None:
            await message.reply("Предмет не выбран.")
        else:
            await message.reply(subject["subject"])


    def _format_history(self, history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        Форматирует историю для передачи модели. 
        История передается в формате [{'role': 'user/assistant', 'content': 'текст'}].
        """
        formatted = []
        for entry in history:
            # Check if 'sender' key exists

            role = "user" if entry["role"] == "user" else "assistant"
            formatted.append({"role": role, "content": entry["content"]})
        return formatted

    async def render_latex_formula_as_image(self, formula: str, output_path: str = "formula.png"):
        """
        Преобразует формулу LaTeX в изображение и сохраняет его.
        
        :param formula: Формула в формате LaTeX.
        :param output_path: Путь для сохранения изображения.
        
        """
        print('formula')
        plt.figure(figsize=(6, 2))  # Устанавливаем размер изображения
        plt.text(0.5, 0.5, f"${formula}$", fontsize=20, ha='center', va='center')  # Рендеринг формулы
        plt.axis("off")  # Убираем оси
        plt.savefig(output_path, dpi=300, bbox_inches='tight', pad_inches=0.1)  # Сохраняем изображение
        plt.close()  # Закрываем график, чтобы освободить память


    async def handle_equation(self, message: Message):
        """
        Обработчик уравнения: распознает текст и показывает его пользователю.
        """
        try:
            logging.info("Начало обработки уравнения.")
            # Проверка на текст или изображение
            if message.text:
                recognized_text = message.text[6:]
                logging.info(f"Текст уравнения : {recognized_text}")
            elif message.photo:
                photo = message.photo[-1]
                file_info = await self.bot.get_file(photo.file_id)
                file_path = file_info.file_path
                file = await self.bot.download_file(file_path)
                logging.info("Изображение загружено, начало распознавания текста.")

                if file is None:
                    await message.reply("Не удалось загрузить изображение.")
                    return

                try:
                    img = Image.open(file)
                    print(img)
                    recognized_text = self.ocr.infer_image(img, 0)
                    logging.info(f"Распознанный текст с изображения: {recognized_text}")
                    if not recognized_text.strip():
                        await message.reply("Не удалось распознать текст на изображении.")
                        return
                except Exception as e:
                    logging.error(f"Ошибка при открытии изображения: {str(e)}")
                    await message.reply("Произошла ошибка при обработке изображения.")
                    return
            else:
                await message.reply("Please send a text or image with an equation.")
                return

            keyboard_builder = InlineKeyboardBuilder()
            keyboard_builder.add(InlineKeyboardButton(text="Подтвердить", callback_data="confirm_equation"))
            keyboard_builder.add(InlineKeyboardButton(text="Отклонить", callback_data="reject_equation"))
            keyboard = keyboard_builder.as_markup()
            recognized_text = recognized_text.strip('$')

            # Сохранение распознанного текста
            chat_id = message.chat.id
            self.db.set_temp_data(chat_id, "equation_text", recognized_text)

            # Рендеринг формулы в изображение
            formula_path = "formula.png"
            await self.render_latex_formula_as_image(recognized_text, formula_path)

            # Отправка изображения пользователю
            with open(formula_path, "rb") as photo:
                input_file = BufferedInputFile(photo.read(), filename="formula.png")
                await message.reply_photo(input_file, caption="Распознанное уравнение:" , reply_markup=keyboard)
        except Exception as e:
            logging.error(f"An error occurred: {str(e)}")
            await message.reply(f"An error occurred: {str(e)}")



    async def render_latex_formula_as_image(self, formula: str, output_path: str = "formula.png"):
        """
        Преобразует формулу LaTeX в изображение и сохраняет его.
        
        :param formula: Формула в формате LaTeX
        :param output_path: Путь для сохранения изображения
        """
        # Настройка размера изображения
        plt.figure(figsize=(6, 2))  # Размер изображения (ширина, высота в дюймах)
        plt.text(0.5, 0.5, f"${formula}$", fontsize=20, ha='center', va='center')  # Добавление формулы
        plt.axis("off")  # Убираем оси
        plt.savefig(output_path, dpi=300, bbox_inches='tight', pad_inches=0.1)  # Сохранение изображения
        plt.close()  # Закрытие графика для освобождения памяти


    async def confirm_equation_handler(self, callback: CallbackQuery):
        provider = self.providers.get("ruadapt_qwen2.5_3b_ext_u48_instruct_v4_gguf")
        print(provider)
        print(type(provider))
        if provider is None:
            await callback.message.reply("Ошибка: Провайдер не найден.")
            return
        elif provider.model_name != 'gpt-4o-mini':
            try:
                chat_id = callback.message.chat.id
                equation_text = self.db.get_temp_data(chat_id, "equation_text")
                if not equation_text:
                    await callback.message.reply("Ошибка: Уравнение не найдено.")
                    return

                # 1. Поиск оптимального пути решения
                solution_paths = await self._find_optimal_solution_path(equation_text, provider=provider )
                print('################################################################### солюшен патх')
                print(f"------------------------------------------------------{ solution_paths}----------------------------------------------------------")
                best_path = solution_paths[0] if solution_paths else None

                # 2. Генерация решения по оптимальному пути
                solution_steps = await self._generate_solution_steps(equation_text, best_path, provider=provider)
                print('################################################################### солюшен степ')
                print(f"------------------------------------------------------{solution_steps}----------------------------------------------------------")
                
                # 3. Проверка промежуточных результатов
                verified_steps = await self._verify_intermediate_steps(solution_steps, provider=provider)
                print('################################################################### верифаед степы')
                print(f"------------------------------------------------------{verified_steps}----------------------------------------------------------")
                
                # 4. Адаптация подхода если есть ошибки
                if any(not step["is_correct"] for step in verified_steps):
                    previous_attempts = [step for step in verified_steps if not step["is_correct"]]
                    adapted_solution = await self._adapt_solution_approach(equation_text, previous_attempts, provider=provider)
                    solution_steps = await self._generate_solution_steps(equation_text, adapted_solution)
                    verified_steps = await self._verify_intermediate_steps(solution_steps)

                # Форматирование и отправка результата
                formatted_response = self._format_verified_solution(verified_steps)
                await callback.message.reply(
                    f"Уравнение: `{equation_text}`\n\n{formatted_response}",
                    parse_mode=ParseMode.MARKDOWN
                )
                answer = self._finalize_solution(verified_steps)
                await callback.message.reply(
                    f"Последнее решение: `{equation_text}`\n\n{formatted_response}",
                    parse_mode=ParseMode.MARKDOWN
                )


            except Exception as e:
                await callback.message.reply(f"Произошла ошибка: {str(e)}")
        else : 
            try:
                chat_id = callback.message.chat.id
                equation_text = self.db.get_temp_data(chat_id, "equation_text")
                if not equation_text:
                    await callback.message.reply("Ошибка: Уравнение не найдено.")
                    return

                response = await self._query_api(provider, [{"role": "user", "content": equation_text}], system_prompt=provider.system_prompt)
                await callback.message.reply(f"Уравнение: `{equation_text}`\n\nРешение: `{response}`" , parse_mode=ParseMode.MARKDOWN)
            except Exception as solve_error:
                if callback.message and callback.message.text:
                    await callback.message.reply(f"Ошибка при решении уравнения: {str(solve_error)}")
                else:
                    await callback.message.reply(f"Ошибка при решении уравнения: {str(solve_error)}")
            
         

    async def reject_equation_handler(self, callback: CallbackQuery):
        """
        Обработчик отклонения уравнения.
        """
        try:
            if callback.message and callback.message.text:
                await callback.message.reply("Уравнение отклонено. Пожалуйста, отправьте новое уравнение.")
            else:
                await callback.message.answer("Уравнение отклонено. Пожалуйста, отправьте новое уравнение.")
        except Exception as e:
            logging.error(f"An error occurred: {str(e)}")
            if callback.message:
                await callback.message.answer(f"Произошла ошибка: {str(e)}")

   



    async def generate(self, message: Message) -> None:
        assert message.from_user
        user_id = message.from_user.id
        user_name = self._get_user_name(message.from_user)
        chat_id = user_id
        conv_id = self.db.get_current_conv_id(chat_id)
        content = await self._build_content(message)
        history = self.db.fetch_conversation(conv_id)
        formatted_history = self._format_history(history)
        full_context = formatted_history + [{"role": "user", "content": content}]
        print('--------', content, '----------------')
        self.db.save_user_message(content, conv_id=conv_id, user_id=user_id, user_name=user_name)

        placeholder = await message.reply("⏳")
        provider = self.providers["ruadapt_qwen2.5_3b_ext_u48_instruct_v4_gguf"]
        try:
            # Получаем текущий предмет из базы данных
            current_table = self.db.get_current_subject(chat_id)
            
            if current_table['subject'] != None:
          
                table = self.vectordb.open_table(self.subject[current_table['subject']])
                docs = table.search(content, query_type="hybrid").limit(5).to_pandas()["text"].to_list()

                # Prepare the prompt with context
               
                rag_promt = provider.rag_prompt
                prompt = rag_promt.format(context=docs, question=content)
                full_context = formatted_history + [{"role": "user", "content": prompt}]
                print(rag_promt)
                system_prompt = provider.rag_prompt
            else:
                system_prompt = provider.system_prompt


            # Query the API
            answer = await self._query_api(provider=provider, messages=full_context, system_prompt=system_prompt)

            # Split and send the answer
            answer_parts = _split_message(answer, output_chunk_size=self.config.output_chunk_size)
            new_message = await _edit_text(placeholder, answer_parts[0])
            for part in answer_parts[1:]:
                new_message = await _reply(message, part)

            markup = self.likes_kb.as_markup()
            await _edit_text(new_message, answer_parts[-1], reply_markup=markup)

            self.db.save_assistant_message(
                content=answer,
                conv_id=conv_id,
                message_id=new_message.message_id,
                system_prompt=provider.system_prompt,
                rag_promt = provider.rag_prompt,
                reply_user_id=user_id,
            )
        except Exception as e:
            error_message = traceback.format_exc()
            logging.error(f"An error occurred: {error_message}")
            await placeholder.edit_text(f'Что-то пошло не так: {str(e)}')



    @staticmethod
    async def _query_api(
        provider: LLMProvider,
        messages: ChatMessages,
        system_prompt: str,
        num_retries: int = 2,
        **kwargs: Any
    ) -> str:
        assert messages
        if messages[0]["role"] != "system" and system_prompt.strip():
            messages.insert(0, {"role": "system", "content": system_prompt})

        if messages[0]["role"] == "system":
            system_message = messages[0]["content"]
            messages = messages[1:]
            messages[0]["content"] = system_message + "\n\n" + messages[0]["content"]

        
        casted_messages = [cast(ChatCompletionMessageParam, message) for message in messages]
        answer: Optional[str] = None
        for _ in range(num_retries):
            try:
                chat_completion = await provider.api.chat.completions.create(
                    model=provider.model_name, messages=casted_messages, **kwargs
                )
                assert chat_completion.choices, str(chat_completion)
                assert chat_completion.choices[0].message.content, str(chat_completion)
                assert isinstance(chat_completion.choices[0].message.content, str), str(chat_completion)
                answer = chat_completion.choices[0].message.content
                break
            except Exception:
                traceback.print_exc()
                continue
        assert answer
       
        return answer
    


    @staticmethod
    async def _query_api_struct_out(
        scheme: BaseModel,
        provider: LLMProvider,
        messages: ChatMessages,
        system_prompt: str,
        num_retries: int = 2,
        **kwargs: Any
    ) -> str:
    #TODO
        pass
        # assert messages
        # if messages[0]["role"] != "system" and system_prompt.strip():
        #     messages.insert(0, {"role": "system", "content": system_prompt})

        # if messages[0]["role"] == "system":
        #     system_message = messages[0]["content"]
        #     messages = messages[1:]
        #     messages[0]["content"] = system_message + "\n\n" + messages[0]["content"]

        
        # casted_messages = [cast(ChatCompletionMessageParam, message) for message in messages]
        # answer: Optional[str] = None
        # for _ in range(num_retries):
        #     try:
        #         chat_completion = await provider.api.chat.completions.create(
        #             model=provider.model_name, messages=casted_messages, **kwargs
        #         )
        #         assert chat_completion.choices, str(chat_completion)
        #         assert chat_completion.choices[0].message.content, str(chat_completion)
        #         assert isinstance(chat_completion.choices[0].message.content, str), str(chat_completion)
        #         answer = chat_completion.choices[0].message.content
        #         break
        #     except Exception:
        #         traceback.print_exc()
        #         continue
        # assert answer
       
        # return answer

    async def _build_content(self, message: Message) -> Union[None, str, List[Dict[str, Any]]]:
        assert message.text
        text = message.text
        assert self.bot_info
        assert self.bot_info.username
        return text

   


    async def save_feedback_handler(self, callback: CallbackQuery) -> None:
        assert callback.from_user
        assert callback.message
        assert callback.data
        user_id = callback.from_user.id
        message_id = callback.message.message_id
        feedback = callback.data.split(":")[1]
        self.db.save_feedback(feedback, user_id=user_id, message_id=message_id)
        await self.bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id, message_id=message_id, reply_markup=None
        )


    def _get_user_name(self, user: User) -> str:
        return str(user.full_name) if user.full_name else str(user.username)


    def _truncate_text(self, text: str) -> str:
        if self.config.output_chunk_size and len(text) > self.config.output_chunk_size:
            text = text[: self.config.output_chunk_size] + "... truncated"
        return text

    async def start_polling(self) -> None:
        # Initialize the scheduler with the configured timezone
        self.scheduler = AsyncIOScheduler(timezone=self.config.timezone)
        
        # Start the scheduler
        self.scheduler.start()
        
        # Fetch and store bot information
        self.bot_info = await self.bot.get_me()
        
        # Start polling
        await self.dp.start_polling(self.bot)


    async def _find_optimal_solution_path(self, problem: str , provider: LLMProvider) -> List[str]:
        """Поиск оптимального пути решения через генерацию нескольких вариантов"""
        if not provider:
            return []
        
        system_prompt = """Сгенерируй несколько  способов  для решения .
        Для каждого способа:
        1. Опиши стратегию
        2. Выпиши список шагов
        3. Вычисли сложность
        4.  Выяви возможные  сложности
        
        Выдели самое эффективное  решение  по критериям:
        - Количество  шагов
        - Вычислительную  сложность
        - Вероятность  правильного решения
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": problem}
        ]
        
        response = await self._query_api(provider, messages, system_prompt)
        return self._parse_solution_paths(response)

    async def _verify_intermediate_steps(self, steps: List[Dict[str, str]], provider:LLMProvider) -> List[Dict[str, Any]]:
        """Проверка каждого промежуточного шага решения"""
        verified_steps = []
        for step in steps:
            verification = {
                "step": step,
                "is_correct": await self._verify_single_step(step, provider=provider),
            }
            verified_steps.append(verification)
        return verified_steps

    async def _verify_single_step(self, step: Dict[str, str], provider: LLMProvider) -> bool:
        """Проверка корректности отдельного шага"""
        if not provider:
            return False
        
        system_prompt = """Проверь шаг решения на наличие ошибок.
        Проверяй:
        1. Математические операции
        2. Алгебраические преобразования
        3. Логику 
        4. Промежуточные результаты
        
        Ответь в следующем формате:
        VERIFICATION:
        - Математические операции: [CORRECT/INCORRECT] с объяснением
        - Алгебраические преобразования: [CORRECT/INCORRECT] с объяснением
        - Логика: [CORRECT/INCORRECT] с объяснением
        - Промежуточные результаты: [CORRECT/INCORRECT] с объяснением
        
        FINAL_VERDICT: [CORRECT/INCORRECT]
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(step)}
        ]
        
        try:
            response = await self._query_api(provider, messages, system_prompt)
            
            # Парсим ответ для получения детальной информации
            verification_details = {}
            final_verdict = False
            
            for line in response.split('\n'):
                line = line.strip()
                if not line:
                    continue
                    
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    if 'FINAL_VERDICT' in key:
                        final_verdict = 'CORRECT' in value.upper()
                    elif 'VERIFICATION' not in key:
                        verification_details[key] = value
            
            # Сохраняем детали проверки в шаге
            step['verification_details'] = verification_details
            
            return final_verdict
        except Exception as e:
            logging.error(f"Error verifying step: {str(e)}")
            return False
        
    async def _finalize_solution(self, verified_steps: List[Dict[str, Any]] , provider:LLMProvider) -> str:
        pass

    async def _adapt_solution_approach(self, problem: str, previous_attempts: List[Dict[str, Any]], provider:LLMProvider) -> str:
        """Адаптация подхода к решению на основе предыдущих попыток"""
        if not provider:
            return ""
        
        system_prompt = """На основе прошлых попыток решения, адаптируй решение:
        1. Избегать прошлых  ошибок
        2. Использовать  упешную  стратегию
        3. Оптимизировать путь  решения
        4. Учитывать альтернативные  методы
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Задача: {problem}\nПрошлые попытки: {previous_attempts}"}
        ]
        
        return await self._query_api(provider, messages, system_prompt)

    def _format_verified_solution(self, verified_steps: List[Dict[str, Any]]) -> str:
        """Форматирование решения с учетом проверки каждого шага"""
        formatted = "📝 Решение:\n\n"
        
        # Если verified_steps пустой или содержит только строку, возвращаем её как есть
        if not verified_steps or (len(verified_steps) == 1 and isinstance(verified_steps[0], str)):
            solution = verified_steps[0] if verified_steps else "Решение не найдено"
            # Экранируем специальные символы для Telegram
            solution = solution.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
            return solution
            
        for i, step_data in enumerate(verified_steps, 1):
            formatted += f"🔹 Шаг {i}:\n"
            
            # Если шаг - это строка, выводим её как есть
            if isinstance(step_data, str):
                # Экранируем специальные символы для Telegram
                step_text = step_data.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                formatted += f"{step_text}\n\n"
                continue
                
            step = step_data.get('step', {})
            
            # Добавляем объяснение шага
            if 'explanation' in step:
                explanation = step['explanation'].replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                formatted += f"📊 Объяснение: {explanation}\n"
            
            # Добавляем вычисления
            if 'calculation' in step:
                calculation = step['calculation'].replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                formatted += f"📌 Вычисления: {calculation}\n"
            
            # Добавляем результаты проверки
            if step_data.get('is_correct', False):
                formatted += "✅ Шаг проверен и корректен\n"
            else:
                formatted += "⚠️ Шаг требует проверки\n"
                
                # Добавляем детали проверки
                if 'verification_details' in step:
                    formatted += "🔍 Детали проверки:\n"
                    for key, value in step['verification_details'].items():
                        key = key.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                        value = value.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                        formatted += f"  • {key}: {value}\n"
            
            formatted += "\n"
        
        return formatted

    async def _generate_solution_steps(self, equation: str, solution_path: str ,provider:LLMProvider) -> List[Dict[str, str]]:
        """Генерация пошагового решения уравнения по заданному пути решения"""
        if not provider:
            return []
        
        system_prompt = """Сгенерируй  решение для этой задачи рассуждай шаг за шагом.
        Для каждого шага:
        1. Объясняи что ты делаешь и почему
        2. Показывай математические операции
        3. Включай промежуточные  результаты
        4. Проверяй шаг на корректность
        
        Каждый шаг оформляй в таком стиле:
        {
            "explanation": "Четкое объяснение шага",
            "calculation": "Математические операции и их результат",
            "verification": "Как проверить этот шаг"
        }

        В конце добавь финальный ответ в формате:
        {
            "final_answer": "Итоговый ответ уравнения",
        }
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Задача: {equation}\nПуть решения: {solution_path}"}
        ]
        
        try:
            response = await self._query_api(provider, messages, system_prompt)
            # Парсим ответ в список шагов
            steps = []
            current_step = {}
            final_answer = None
            
            for line in response.split('\n'):
                line = line.strip()
                if not line:
                    continue
                    
                if '"final_answer":' in line:
                    # Нашли финальный ответ
                    final_answer = line.split('"final_answer":')[1].strip().strip('",')
                    continue
                    
                if '"explanation":' in line:
                    if current_step:
                        steps.append(current_step)
                    current_step = {"explanation": line.split('"explanation":')[1].strip().strip('",')}
                elif '"calculation":' in line:
                    current_step["calculation"] = line.split('"calculation":')[1].strip().strip('",')
                elif '"verification":' in line:
                    current_step["verification"] = line.split('"verification":')[1].strip().strip('",')
            
            if current_step:
                steps.append(current_step)
            
            # Добавляем финальный ответ как отдельный шаг
            if final_answer:
                steps.append({
                    "explanation": "Финальный ответ уравнения",
                    "calculation": final_answer,
                    "verification": "Проверка подстановкой в исходное уравнение"
                })
            
            return steps
        except Exception as e:
            logging.error(f"Error generating solution steps: {str(e)}")
            return []

    def _parse_solution_paths(self, response: str) -> List[str]:
        """Парсинг ответа LLM в список путей решения"""
        try:
            # Разбиваем ответ на отдельные подходы
            approaches = []
            current_approach = []
            
            for line in response.split('\n'):
                line = line.strip()
                if not line:
                    continue
                    
                # Если строка начинается с цифры и точки, это новый подход
                if line[0].isdigit() and '. ' in line[:5]:
                    if current_approach:
                        approaches.append('\n'.join(current_approach))
                    current_approach = [line]
                else:
                    current_approach.append(line)
            
            # Добавляем последний подход
            if current_approach:
                approaches.append('\n'.join(current_approach))
            
            # Если подходы не найдены, возвращаем весь ответ как один подход
            if not approaches:
                approaches = [response]
            
            return approaches
        except Exception as e:
            logging.error(f"Error parsing solution paths: {str(e)}")
            return [response]  # В случае ошибки возвращаем исходный ответ как один подход


def main(
    # bot_config_path: str,
    # providers_config_path: str,
    # db_path: str,
) -> None:
    logging.info("Запуск основного процесса...")
    bot = LlmBot(
        bot_config_path='configs/bot.json',
        providers_config_path='configs/provider.json',
        db_path='postgresql://postgres:1234@localhost:5432/postgres',
        db_vector_path='~/math',
        subject_path='configs/subject_path.json'
    )
    asyncio.run(bot.start_polling())
    logging.info("Бт завершил работу.")


if __name__ == "__main__":
    fire.Fire(main)

