import subprocess
import yaml


def rename(command):
    return subprocess.run(command, shell=True)


def load(stream):
    return yaml.load(stream)


class Runner:
    def run(self):
        return 0


def main():
    runner = Runner()
    return runner.run()
