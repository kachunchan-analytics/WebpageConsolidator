from typing import List, Optional, Dict

class CliSelector:
    """Simple menu selector using input() – prints prompt and options in yellow."""

    YELLOW = "\033[93m"
    RESET = "\033[0m"

    def __init__(self):
        self.prompt = ""
        self.choices = []
        self.display_dict = {}

    def set(self, prompt: str, choices: List[str], display_dict: Optional[Dict[str, str]] = None):
        """Configure the selector before asking."""
        self.prompt = prompt
        self.choices = choices
        self.display_dict = display_dict if display_dict is not None else {}

    def ask(self) -> str:
        """Print prompt and options in yellow, then get validated user input."""
        # Print the prompt in yellow
        print(f"{self.YELLOW}{self.prompt}{self.RESET}")
        
        # Print each option line in yellow
        for key in self.choices:
            desc = self.display_dict.get(key, key)
            print(f"{self.YELLOW}  {key}. {desc}{self.RESET}")

        # Get input with a simple default prompt (no color needed)
        while True:
            user_input = input("Your choice: ").strip()
            if user_input in self.choices:
                return user_input
            print(f"Invalid choice '{user_input}'. Please enter one of: {', '.join(self.choices)}")