import argparse
import asyncio
import json
import pathlib
import platform
import subprocess
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Tuple
from uuid import uuid4

from aiohttp import web, WSMsgType


PlayerID = str
GameID = str
Board = List[List[int]]


@dataclass
class Player:
    """Represents a player in the game."""

    id: PlayerID
    ws: web.WebSocketResponse
    name: str
    game_id: Optional[GameID] = None
    wants_hints: bool = False


@dataclass
class GameState:
    """Represents the current state of a game."""

    id: GameID
    player1: PlayerID
    player2: PlayerID
    current_turn: PlayerID
    board: Board
    moves: str
    status: str  # 'active', 'finished', 'draw'
    winner: Optional[PlayerID] = None


class Connect4Solver:
    """Handles position analysis using external C++ solver."""

    def __init__(self, solver_path: str = None):
        if solver_path is None:
            solver_path = "./c4solver-mac" if platform.system() == "Darwin" else "./c4solver"
        self.solver_path = solver_path

    async def analyse_position(self, position: str) -> dict:
        """Analyzes a Connect 4 position using the C++ solver."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.solver_path,
                "-a",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate(input=f"{position}\n".encode())

            if process.returncode != 0:
                raise ValueError(f"Solver error: {stderr.decode()}")

            output = stdout.decode().strip().split()
            if len(output) < 8:
                raise ValueError("Invalid solver output")

            scores = [
                int(score) if score.lstrip("-").isdigit() else None
                for score in output[1:8]
            ]

            return {
                "position": position,
                "analysis": {
                    "columns": {
                        str(i + 1): {"score": score, "valid": score != -1000}
                        for i, score in enumerate(scores)
                    }
                },
            }
        except (subprocess.SubprocessError, ValueError) as e:
            raise web.HTTPBadRequest(text=str(e))


class Connect4Game:
    """Core game logic implementation."""

    BOARD_ROWS = 6
    BOARD_COLS = 7
    WINNING_LENGTH = 4

    def __init__(self):
        self.players: Dict[PlayerID, Player] = {}
        self.games: Dict[GameID, GameState] = {}
        self.waiting_player: Optional[PlayerID] = None
        self.solver = Connect4Solver()

    def create_empty_board(self) -> Board:
        """Creates an empty game board."""
        return [[0 for _ in range(self.BOARD_COLS)] for _ in range(self.BOARD_ROWS)]

    def create_game(self, player1_id: PlayerID, player2_id: PlayerID) -> GameState:
        """Creates a new game between two players."""
        game_id = str(uuid4())
        game = GameState(
            id=game_id,
            player1=player1_id,
            player2=player2_id,
            current_turn=player1_id,
            board=self.create_empty_board(),
            moves="",
            status="active",
        )
        self.games[game_id] = game
        self.players[player1_id].game_id = game_id
        self.players[player2_id].game_id = game_id
        return game

    def get_game_state(self, game_id: GameID) -> dict:
        """Returns the current game state with player names."""
        game = self.games[game_id]
        return {
            **asdict(game),
            "player1_name": self.players[game.player1].name,
            "player2_name": self.players[game.player2].name,
        }

    def is_valid_move(self, game: GameState, column: int) -> bool:
        """Checks if a move is valid in the given column."""
        return 0 <= column < self.BOARD_COLS and game.board[0][column] == 0

    def make_move(self, game: GameState, column: int) -> bool:
        """Attempts to make a move and updates the game state."""
        if not self.is_valid_move(game, column):
            return False

        row = self._find_empty_row(game.board, column)
        player_number = 1 if game.current_turn == game.player1 else 2
        game.board[row][column] = player_number
        game.moves += str(column + 1)

        if self.check_win(game.board, row, column, player_number):
            game.status = "finished"
            game.winner = game.current_turn
        elif self._is_board_full(game.board):
            game.status = "draw"
        else:
            game.current_turn = (
                game.player2 if game.current_turn == game.player1 else game.player1
            )

        return True

    def _find_empty_row(self, board: Board, column: int) -> int:
        """Finds the lowest empty row in the given column."""
        for row in range(self.BOARD_ROWS - 1, -1, -1):
            if board[row][column] == 0:
                return row
        raise ValueError("Column is full")

    def _is_board_full(self, board: Board) -> bool:
        """Checks if the board is completely filled."""
        return all(board[0][col] != 0 for col in range(self.BOARD_COLS))

    def check_win(self, board: Board, row: int, col: int, player: int) -> bool:
        """Checks if the last move resulted in a win."""
        directions: List[Tuple[int, int]] = [(0, 1), (1, 0), (1, 1), (1, -1)]

        for dx, dy in directions:
            count = 1
            # Check both directions
            for multiplier in (1, -1):
                x, y = col + dx * multiplier, row + dy * multiplier
                while (
                    0 <= x < self.BOARD_COLS
                    and 0 <= y < self.BOARD_ROWS
                    and board[y][x] == player
                ):
                    count += 1
                    x, y = x + dx * multiplier, y + dy * multiplier

            if count >= self.WINNING_LENGTH:
                return True
        return False


class GameServer:
    """Handles WebSocket connections and game coordination."""

    def __init__(self):
        self.game = Connect4Game()

    async def register_player(
        self, ws: web.WebSocketResponse, name: str, wants_hints: bool
    ) -> Player:
        """Registers a new player connection."""
        player_id = str(uuid4())
        player = Player(id=player_id, ws=ws, name=name, wants_hints=wants_hints)
        self.game.players[player_id] = player
        return player

    async def handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        """Handles incoming WebSocket connections."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    print(
                        f"WebSocket connection closed with exception {ws.exception()}"
                    )
        finally:
            await self._handle_disconnection(ws)

        return ws

    async def _handle_message(self, ws: web.WebSocketResponse, message: str):
        """Processes incoming WebSocket messages."""
        try:
            data = json.loads(message)
            message_type = data["type"]

            if message_type == "register":
                await self._handle_registration(ws, data)
            elif message_type == "move":
                await self._handle_move(ws, data)
            else:
                await ws.send_json({"type": "error", "message": "Unknown message type"})

        except Exception as e:
            await ws.send_json({"type": "error", "message": str(e)})

    async def _handle_registration(self, ws: web.WebSocketResponse, data: dict):
        """Handles player registration."""
        player = await self.register_player(
            ws, data["name"], data.get("wants_hints", False)
        )

        if self.game.waiting_player is None:
            self.game.waiting_player = player.id
            await ws.send_json(
                {"type": "waiting", "message": "Waiting for opponent..."}
            )
        else:
            opponent_id = self.game.waiting_player
            self.game.waiting_player = None
            game = self.game.create_game(opponent_id, player.id)
            await self.broadcast_game_state(game.id)

    async def _handle_move(self, ws: web.WebSocketResponse, data: dict):
        """Handles player moves."""
        player = next(p for p in self.game.players.values() if p.ws == ws)
        game = self.game.games[player.game_id]

        if game.current_turn != player.id:
            await ws.send_json({"type": "error", "message": "Not your turn"})
            return

        if self.game.make_move(game, data["column"]):
            await self.broadcast_game_state(game.id)
            await self._send_hints_if_needed(game)
        else:
            await ws.send_json({"type": "error", "message": "Invalid move"})

    async def _handle_disconnection(self, ws: web.WebSocketResponse):
        """Handles player disconnection."""
        for player_id, player in list(self.game.players.items()):
            if player.ws == ws:
                if player.game_id:
                    game = self.game.games[player.game_id]
                    game.status = "finished"
                    game.winner = (
                        game.player2 if player_id == game.player1 else game.player1
                    )
                    await self.broadcast_game_state(game.id)
                if self.game.waiting_player == player_id:
                    self.game.waiting_player = None
                del self.game.players[player_id]
                break

    async def _send_hints_if_needed(self, game: GameState):
        """Sends hints to the current player if enabled."""
        if game.status == "active" and self.game.players[game.current_turn].wants_hints:
            analysis = await self.game.solver.analyse_position(game.moves)
            await self.game.players[game.current_turn].ws.send_json(
                {"type": "hint", "analysis": analysis}
            )

    async def broadcast_game_state(self, game_id: GameID):
        """Broadcasts the current game state to both players."""
        game_state = self.game.get_game_state(game_id)
        for player_id in [game_state["player1"], game_state["player2"]]:
            await self.game.players[player_id].ws.send_json(
                {"type": "game_state", "state": game_state}
            )


routes = web.RouteTableDef()


@routes.get("/")
async def index(request: web.Request) -> web.FileResponse:
    """Serves the index.html file."""
    return web.FileResponse(pathlib.Path("static/index.html"))


@routes.get("/analyse/{position}")
async def analyse(request: web.Request) -> web.Response:
    """Analyzes a Connect 4 position."""
    position = request.match_info["position"]

    if not all(c in "1234567" for c in position):
        raise web.HTTPBadRequest(text="Position must only contain digits 1-7")

    solver = Connect4Solver()
    result = await solver.analyse_position(position)
    return web.json_response(result)


def main():
    """Application entry point."""
    parser = argparse.ArgumentParser(description="Connect 4 Game Server")
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    args = parser.parse_args()

    game_server = GameServer()
    app = web.Application()
    app.add_routes(
        [
            web.get("/", index),
            web.get("/ws", game_server.handle_connection),
            web.get("/analyse/{position}", analyse),
        ]
    )

    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
