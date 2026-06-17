import enum

import attr

from labgrid.factory import target_factory
from labgrid.strategy.common import Strategy, StrategyError


class Status(enum.Enum):
    unknown = 0
    off = 1
    emmc = 2


@target_factory.reg_driver
@attr.s(eq=False)
class K3PicoITXBootStrategy(Strategy):
    """K3PicoITXBootStrategy - Strategy for SpacemiT K3 Pico-ITX (EDK II, no U-Boot prompt)"""
    bindings = {
        "power": "PowerProtocol",
        "console": "ConsoleProtocol",
        "shell": "ShellDriver",
        "tftp": "TFTPProviderDriver",
    }

    status = attr.ib(default=Status.unknown)

    def transition(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.unknown:
            raise StrategyError(f"cannot transition to {status}")
        if status == self.status:
            return
        if status == Status.off:
            self.target.deactivate(self.console)
            self.target.activate(self.power)
            self.power.off()
        elif status == Status.emmc:
            self.transition(Status.off)
            self.target.activate(self.console)
            self.power.cycle()
            self.target.activate(self.shell)
        else:
            raise StrategyError(f"no transition from {self.status} to {status}")
        self.status = status

    def force(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.off:
            self.target.activate(self.power)
        elif status == Status.emmc:
            self.target.activate(self.shell)
        else:
            raise StrategyError(f"cannot force to {status}")
        self.status = status
