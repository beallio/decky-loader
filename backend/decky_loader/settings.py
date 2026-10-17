from json import dump, load
from os import chmod, fsync, mkdir, path, listdir, rename, replace, stat, unlink
from tempfile import NamedTemporaryFile
from typing import Any, Dict
from .localplatform.localplatform import chown, file_owner, get_chown_plugin_path
from .enums import UserType

from .helpers import get_homebrew_path


class SettingsManager:
    def __init__(self, name: str, settings_directory: str | None = None) -> None:
        wrong_dir = get_homebrew_path()
        if settings_directory == None:
            settings_directory = path.join(wrong_dir, "settings")

        self.path = path.join(settings_directory, name + ".json")

        #Create the folder with the correct permission
        if not path.exists(settings_directory):
            mkdir(settings_directory)

        #Copy all old settings file in the root directory to the correct folder
        for file in listdir(wrong_dir):
            if file.endswith(".json"):
                rename(path.join(wrong_dir,file),
                       path.join(settings_directory, file))
                self.path = path.join(settings_directory, name + ".json")


        #If the owner of the settings directory is not the user, then set it as the user:
        expected_user = UserType.HOST_USER if get_chown_plugin_path() else UserType.EFFECTIVE_USER
        if file_owner(settings_directory) != expected_user:
            chown(settings_directory, expected_user, False)

        self.settings: Dict[str, Any] = {}

        try:
            open(self.path, "x", encoding="utf-8")
        except FileExistsError as _:
            self.read()
            pass

    def read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                self.settings = load(file)
        except Exception as e:
            print(e)
            pass

    def commit(self):
        # Write a complete copy first, then swap it in. A crash or power loss
        # during the write leaves the previous settings file intact.
        directory = path.dirname(self.path)
        try:
            mode = stat(self.path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=f".{path.basename(self.path)}.", suffix=".tmp", delete=False
        ) as file:
            temporary = file.name
            try:
                dump(self.settings, file, indent=4, ensure_ascii=False)
                file.flush()
                fsync(file.fileno())
            except BaseException:
                file.close()
                unlink(temporary)
                raise
        try:
            chmod(temporary, mode)
            replace(temporary, self.path)
        except BaseException:
            try:
                unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def getSetting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def setSetting(self, key: str, value: Any) -> Any:
        self.settings[key] = value
        self.commit()
