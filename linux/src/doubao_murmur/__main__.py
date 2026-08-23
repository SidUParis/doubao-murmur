"""Entry point for Open Voice Input Linux."""

import logging
import sys


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from doubao_murmur.app import DoubaoMurmurApp

    app = DoubaoMurmurApp()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
