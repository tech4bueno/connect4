import argparse
import asyncio
import json
import pathlib
import subprocess
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List

from aiohttp import web, WSMsgType

routes = web.RouteTableDef()


class Connect4Solver:
    def __init__(self, solver_path="./c4solver"):
        self.solver_path = solver_path

    async def analyse_position(self, position: str) -> dict:
        """
        Analyse a Connect 4 position using the C++ solver
        Args:
            position: string of numbers 1-7 representing moves from left to right
        Returns:
            Dictionary containing analysis of the position
        """
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
                raise ValueError("Invalid request")

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


solver = Connect4Solver()


@routes.get("/analyse/{position}")
async def analyse(request):
    """
    Endpoint to analyse a Connect 4 position
    Example: GET /analyse/44 (analyses position where both players played in column 4)
    """
    position = request.match_info["position"]

    if not all(c in "1234567" for c in position):
        raise web.HTTPBadRequest(text="Position must only contain digits 1-7")

    result = await solver.analyse_position(position)
    return web.json_response(result)


@routes.get("/")
async def index(request):
    """Root endpoint serving index.html"""
    return web.FileResponse(pathlib.Path('static/index.html'))

@dataclass
class Player:
    id: str
    ws: web.WebSocketResponse
    name: str
    game_id: Optional[str] = None
    wants_hints: bool = False


@dataclass
class GameState:
    id: str
    player1: str  # Player ID
    player2: str  # Player ID
    current_turn: str  # Player ID
    board: List[List[int]]  # 0: empty, 1: player1, 2: player2
    moves: str  # String of moves for solver
    status: str  # 'active', 'finished', 'draw'
    winner: Optional[str] = None  # Player ID of winner


class Connect4Game:
    def __init__(self):
        self.players: Dict[str, Player] = {}
        self.games: Dict[str, GameState] = {}
        self.waiting_player: Optional[str] = None
        self.solver = Connect4Solver()

    def create_empty_board(self) -> List[List[int]]:
        return [[0 for _ in range(7)] for _ in range(6)]

    def create_game(self, player1_id: str, player2_id: str) -> GameState:
        game_id = str(uuid.uuid4())
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

    def get_game_state(self, game_id: str) -> dict:
        game = self.games[game_id]
        return {
            **asdict(game),
            "player1_name": self.players[game.player1].name,
            "player2_name": self.players[game.player2].name,
        }

    def is_valid_move(self, game: GameState, column: int) -> bool:
        return 0 <= column < 7 and game.board[0][column] == 0

    def make_move(self, game: GameState, column: int) -> bool:
        if not self.is_valid_move(game, column):
            return False

        # Find the lowest empty row
        row = 5
        while row >= 0 and game.board[row][column] != 0:
            row -= 1

        # Place the piece
        player_number = 1 if game.current_turn == game.player1 else 2
        game.board[row][column] = player_number
        game.moves += str(column + 1)

        # Check for win
        if self.check_win(game.board, row, column, player_number):
            game.status = "finished"
            game.winner = game.current_turn
        # Check for draw
        elif all(game.board[0][col] != 0 for col in range(7)):
            game.status = "draw"
        else:
            # Switch turns
            game.current_turn = (
                game.player2 if game.current_turn == game.player1 else game.player1
            )

        return True

    def check_win(
        self, board: List[List[int]], row: int, col: int, player: int
    ) -> bool:
        directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
        for dx, dy in directions:
            count = 1
            # Check forward
            x, y = col + dx, row + dy
            while 0 <= x < 7 and 0 <= y < 6 and board[y][x] == player:
                count += 1
                x, y = x + dx, y + dy
            # Check backward
            x, y = col - dx, row - dy
            while 0 <= x < 7 and 0 <= y < 6 and board[y][x] == player:
                count += 1
                x, y = x - dx, y - dy
            if count >= 4:
                return True
        return False


class GameServer:
    def __init__(self):
        self.game = Connect4Game()

    async def register_player(
        self, ws: web.WebSocketResponse, name: str, wants_hints: bool
    ) -> Player:
        player_id = str(uuid.uuid4())
        player = Player(id=player_id, ws=ws, name=name, wants_hints=wants_hints)
        self.game.players[player_id] = player
        return player

    async def handle_connection(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data["type"] == "register":
                        player = await self.register_player(
                            ws, data["name"], data.get("wants_hints", False)
                        )
                        if self.game.waiting_player is None:
                            self.game.waiting_player = player.id
                            await ws.send_json(
                                {
                                    "type": "waiting",
                                    "message": "Waiting for opponent...",
                                }
                            )
                        else:
                            # Start a new game
                            opponent_id = self.game.waiting_player
                            self.game.waiting_player = None
                            game = self.game.create_game(opponent_id, player.id)

                            # Notify both players
                            await self.broadcast_game_state(game.id)

                    elif data["type"] == "move":
                        player = next(
                            p for p in self.game.players.values() if p.ws == ws
                        )
                        game = self.game.games[player.game_id]

                        if game.current_turn != player.id:
                            await ws.send_json(
                                {"type": "error", "message": "Not your turn"}
                            )
                            continue

                        if self.game.make_move(game, data["column"]):
                            await self.broadcast_game_state(game.id)

                            # If hints are enabled and game is still active,
                            # provide hint for next player
                            if (
                                game.status == "active"
                                and self.game.players[game.current_turn].wants_hints
                            ):
                                analysis = await self.game.solver.analyse_position(
                                    game.moves
                                )
                                await self.game.players[game.current_turn].ws.send_json(
                                    {"type": "hint", "analysis": analysis}
                                )
                        else:
                            await ws.send_json(
                                {"type": "error", "message": "Invalid move"}
                            )

                except Exception as e:
                    await ws.send_json({"type": "error", "message": str(e)})

            elif msg.type == WSMsgType.ERROR:
                print(f"WebSocket connection closed with exception {ws.exception()}")

        # Handle disconnection
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

        return ws

    async def broadcast_game_state(self, game_id: str):
        game_state = self.game.get_game_state(game_id)
        for player_id in [game_state["player1"], game_state["player2"]]:
            await self.game.players[player_id].ws.send_json(
                {"type": "game_state", "state": game_state}
            )


def main():
    parser = argparse.ArgumentParser(description='Connect 4 Game Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to bind to')
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
