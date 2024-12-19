# Connect 4

A game of Connect 4 with optional hints enabling perfect gameplay.

Solver courtesy of [Pascal Pons](https://github.com/PascalPons/connect4)

## Design goals

* Mobile-friendly

## Implementation goals

* <1,000 lines of code (excluding solver)
* Single server for simple deployment
* No frontend build step

## Try

Go to https://connect4-pi5t.onrender.com (be patient: first load takes 1min+ on Render's free plan)

## Run

### With [uv](https://docs.astral.sh/uv/)

`uvx --from connect4 connect4`

### With pip

`pip install connect4` and then `connect4`

### With Docker

`docker build -t connect4 .` then `docker run -p 8080:8080 connect4`

## Develop

```
git clone https://github.com/tech4bueno/connect4
pip install -e .[test]
pytest
```

## Deploy

Deploys easily to Render's free plan.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)
