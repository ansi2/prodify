# ------------------------------------------------------------------------------
# Prodify: Product assistant in coding
# (C) IURII TRUKHIN, yuri@trukhin.com, 2024
# Licensed under the Apache License, Version 2.0 (the "License");
# http://www.apache.org/licenses/LICENSE-2.0
#
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
# either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
# ------------------------------------------------------------------------------

import os
import sys
import re
import getpass
import shutil
import queue
import threading
import time

from ollama import chat

# prompt_toolkit for the radio dialog
try:
    from prompt_toolkit import prompt
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign
    from prompt_toolkit.widgets import Dialog, Button, Label, RadioList
    from prompt_toolkit.styles import Style
    from prompt_toolkit.key_binding import KeyBindings
except ImportError:
    print("(C) IURII TRUKHIN, yuri@trukhin.com, 2024")
    print("prompt_toolkit is missing. Please install it: pip install prompt_toolkit")
    sys.exit(1)

# tqdm for progress bar
try:
    from tqdm import tqdm
except ImportError:
    print("tqdm is missing. Please install it: pip install tqdm")
    sys.exit(1)

# rich for pretty console output
try:
    from rich.console import Console
    from rich.markdown import Markdown
except ImportError:
    print("rich is missing. Please install it: pip install rich")
    sys.exit(1)



# tiktoken for token counting
try:
    import tiktoken
except ImportError:
    print("tiktoken is missing. Please install it: pip install tiktoken")
    sys.exit(1)

# Attempt to import langchain + chroma. Otherwise fallback.
try:
    from langchain_ollama import OllamaEmbeddings
    from langchain_chroma import Chroma
except ImportError:
    print("Falling back to langchain_community.")
    from langchain.embeddings import OllamaEmbeddings
    from langchain_community.vectorstores import Chroma

console = Console()

NUM_WORKERS = 4
QUEUE_SIZE = 100
MAX_TOKENS = 128_000
MAX_RETRIES = 5
INITIAL_DELAY = 1.0
K = 100

global_lock = threading.Lock()


# ------------------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------------------

def parse_date_time(index_name: str):
    """
    Attempts to parse a string like 'projectName_YYYYmmdd_HHMMSS_guid'
    and returns (projectName, 'YYYY-mm-dd HH:MM') or a fallback.
    """
    pattern = r"^(?P<name>.+)_(?P<date>\d{8})_(?P<time>\d{6})_(?P<guid>[0-9a-fA-F]+)$"
    match = re.match(pattern, index_name)
    if not match:
        return index_name, ""
    proj = match.group("name") or "project"
    d = match.group("date")
    t = match.group("time")

    yyyy = d[0:4]
    mm = d[4:6]
    dd = d[6:8]
    hh = t[0:2]
    mn = t[2:4]
    return proj, f"{yyyy}-{mm}-{dd} {hh}:{mn}"


def parse_file_update_instructions(answer: str):
    """
    Detects the following block format in the AI's answer:

      [FILE_UPDATE]
      filename: ...
      code:
      ... updated code ...
      [/FILE_UPDATE]

    Returns a list of tuples (filename, new_code).
    """
    pattern = re.compile(
        r"\[FILE_UPDATE\]\s*filename:\s*(.+?)\s*code:\s*(.+?)\[\/FILE_UPDATE\]",
        flags=re.DOTALL
    )
    matches = pattern.findall(answer)
    results = []
    for fname, code in matches:
        fname = fname.strip()
        code = code.strip()
        results.append((fname, code))
    return results


def update_file_contents(file_path: str, new_content: str):
    """
    Overwrites file_path with new_content. Creates directories if needed.
    """
    parent_dir = os.path.dirname(file_path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ------------------------------------------------------------------------------
# Worker class for handling user queries (Q:)
# ------------------------------------------------------------------------------

class AskWorker(threading.Thread):
    """
    A worker thread that processes user queries in the background:
      1) Retrieves relevant documents from retriever
      2) Builds the context prompt
      3) Calls OpenAI
      4) Prints the answer
      5) Checks for [FILE_UPDATE] instructions and applies them if user confirms
    """

    def __init__(self, task_queue, retriever, enc):
        super().__init__()
        self.task_queue = task_queue
        self.retriever = retriever
        self.enc = enc
        self.daemon = True

    def run(self):
        while True:
            item = self.task_queue.get()
            if item is None:
                self.task_queue.task_done()
                break

            query, idx = item
            self.process_query(query, idx)
            self.task_queue.task_done()

    def process_query(self, query, idx):
        with global_lock:
            console.print(f"\n[bold](Processing question #{idx})[/bold] Q: {query}", style="dim")

        with tqdm(total=2, desc=f"Processing question #{idx}", unit="step") as pbar:
            # 1) Retrieve docs
            docs = []
            try:
                docs = self.retriever.invoke(query)
            except Exception as e:
                with global_lock:
                    console.print(f"Error retrieving docs: {e}", style="bold red")
                return
            pbar.update(1)

            # 2) Build prompt
            system_prefix = "You are a code assistant. Use the provided context:\n\nContext:\n"
            suffix = f"\nQuestion: {query}"
            base_msg = f"{system_prefix}<CONTEXT_PLACEHOLDER>{suffix}"
            base_tokens = len(self.enc.encode(base_msg))

            context_parts = []
            current_tokens = base_tokens
            for i, doc in enumerate(docs, start=1):
                source = doc.metadata.get("source", "unknown")
                piece = f"--- document {i} source: {source} ---\n{doc.page_content}\n\n"
                piece_tokens = len(self.enc.encode(piece))
                if current_tokens + piece_tokens > MAX_TOKENS:
                    break
                context_parts.append(piece)
                current_tokens += piece_tokens

            user_prompt = f"{system_prefix}{''.join(context_parts)}{suffix}"

            # 3) Call Ollama
            delay = INITIAL_DELAY
            answer = None
            for attempt in range(MAX_RETRIES):
                try:
                    resp = chat(
                        model="llama3.2-vision:latest",
                        messages=[{"role": "user", "content": user_prompt}],
                    )
                    answer = resp.message.content
                    break
                except Exception as e:
                    with global_lock:
                        console.print(f"Unexpected error: {e}", style="bold red")
                    return

            pbar.update(1)

        # 4) Print answer
        with global_lock:
            if answer:
                console.print("[dim]\n=== Answer ===[/dim]", style="dim")
                console.print(Markdown(answer))

                # 5) Check for [FILE_UPDATE]
                updates = parse_file_update_instructions(answer)
                for fname, new_code in updates:
                    console.print(
                        f"\n[bold yellow]AI suggests updating file:[/bold yellow] {fname}",
                        style="bold yellow"
                    )
                    console.print("Proposed new content:\n", style="dim")
                    console.print(Markdown(f"```\n{new_code}\n```"))
                    confirm = input("Apply this update? [y/N] ").strip().lower()
                    if confirm == 'y':
                        update_file_contents(fname, new_code)
                        console.print(f"File {fname} has been updated.\n", style="bold green")
            else:
                console.print("No answer was returned. Something went wrong.", style="bold red")


# ------------------------------------------------------------------------------
# A simple radio-list dialog
# ------------------------------------------------------------------------------

def radio_with_three_buttons_dialog(title: str, text: str, values, style=None):
    """
    Shows a RadioList with three buttons: [Use] (green), [Delete] (red), [Exit].
    Returns (selected_index, action) or (None, None) if user presses Esc.
    """
    radio = RadioList(values=values)
    result_index = [None]
    result_action = [None]

    def on_use():
        result_index[0] = radio.current_value
        result_action[0] = "use"
        get_app().exit()

    def on_delete():
        result_index[0] = radio.current_value
        result_action[0] = "delete"
        get_app().exit()

    def on_exit():
        result_index[0] = radio.current_value
        result_action[0] = "exit"
        get_app().exit()

    btn_use = Button(text="Use", handler=on_use)
    btn_use.style = "fg:green"

    btn_del = Button(text="Delete", handler=on_delete)
    btn_del.style = "fg:red"

    btn_exit = Button(text="Exit", handler=on_exit)

    body = HSplit([
        Label(text=title, dont_extend_height=True),
        Label(text=text, dont_extend_height=True),
        radio,
    ])

    dialog = Dialog(
        body=body,
        buttons=[btn_use, btn_del, btn_exit],
        with_background=False
    )

    kb = KeyBindings()

    @kb.add("escape")
    def _(event):
        result_index[0] = None
        result_action[0] = None
        event.app.exit()

    layout = Layout(dialog)
    application = Application(
        layout=layout,
        key_bindings=kb,
        style=style or Style(),
        full_screen=False
    )
    application.run()

    return result_index[0], result_action[0]


# ------------------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------------------

def main():
    console.print("Prodify: Product assistant in coding", style="bold")
    console.print("(C) IURII TRUKHIN, yuri@trukhin.com, 2024\n", style="bold")
   
    base_dir = ".chromadb"
    if not os.path.isdir(base_dir):
        console.print("No indexes found in .chromadb. Exiting.", style="bold red")
        sys.exit(0)

    # Main loop: choosing an index
    while True:
        subfolders = []
        for entry in os.scandir(base_dir):
            if entry.is_dir():
                subfolders.append(entry.name)
        if not subfolders:
            console.print("No indexes found. Exiting.", style="bold red")
            break

        subfolders.sort()
        radio_values = []
        for folder_name in subfolders:
            proj, dt_str = parse_date_time(folder_name)
            label = f"{proj} ({dt_str})" if dt_str else folder_name
            radio_values.append((folder_name, label))

        index_choice, action = radio_with_three_buttons_dialog(
            title="Choose an index from .chromadb",
            text=(
                "Use ↑/↓ to move, Enter to select an item.\n"
                "Then click [Use], [Delete], or [Exit].\n"
                "Press Esc to cancel."
            ),
            values=radio_values,
            style=Style.from_dict({
                "dialog":       "bg:#ffffff #000000",
                "dialog.body":  "bg:#ffffff #000000",
                "dialog.shadow":"bg:#cccccc",
            }),
        )

        if index_choice is None or action is None:
            console.print("\nNo selection was made. Exiting.\n", style="bold yellow")
            break

        chosen_path = os.path.join(base_dir, index_choice)
        console.print(f"You selected: {chosen_path}\n", style="bold")

        if action == "use":
            console.print("Loading index for Q&A...", style="dim")
            try:
                embeddings = OllamaEmbeddings(model="llama3.2-vision:latest")
                db = Chroma(
                    collection_name=index_choice,
                    embedding_function=embeddings,
                    persist_directory=chosen_path
                )
            except Exception as e:
                console.print(f"Error loading {chosen_path}: {e}", style="bold red")
                continue

            retriever = db.as_retriever(
                search_type="similarity",
                search_kwargs={"k": K}
            )
            enc = tiktoken.get_encoding("cl100k_base")

            task_queue = queue.Queue(maxsize=QUEUE_SIZE)
            workers = []
            for _ in range(NUM_WORKERS):
                w = AskWorker(task_queue, retriever, enc)
                w.start()
                workers.append(w)

            console.print(
                "\n[bold dim]You can now ask questions about the project codebase.[/bold dim]\n"
                " - Type your question and press Enter.\n"
                " - Press Ctrl+C to exit.\n",
                style="dim"
            )

            question_counter = 0
            try:
                while True:
                    user_query = input("Q: ")
                    if not user_query.strip():
                        console.print("[gray]Empty question. Try again or Ctrl+C to exit.[/gray]")
                        continue
                    question_counter += 1
                    task_queue.put((user_query, question_counter))
            except KeyboardInterrupt:
                console.print("\nInterrupted by user.\n", style="bold yellow")

            for _ in range(NUM_WORKERS):
                task_queue.put(None)
            task_queue.join()
            for w in workers:
                w.join()

            console.print("Done with Q&A.\n", style="dim")

        elif action == "delete":
            console.print(f"Deleting index folder: {chosen_path}\n", style="bold red")
            try:
                shutil.rmtree(chosen_path)
                console.print(f"Deleted: {chosen_path}\n", style="bold red")
            except Exception as e:
                console.print(f"Error deleting {chosen_path}: {e}", style="bold red")

        elif action == "exit":
            console.print("Exiting program.\n", style="bold yellow")
            sys.exit(0)

    console.print("\nAll done!\n", style="dim")


if __name__ == "__main__":
    main()
