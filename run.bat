@echo off
rem run.bat -- the demo and test targets without make, for a plain cmd.exe.
rem
rem   run.bat demo-vm      real VMs from data\demo_input, one at a time
rem   run.bat demo-kind    one pod OOMKilled by the kernel in the kind cluster
rem   run.bat demo         offline pipeline on synthetic traces
rem   run.bat demo DIR     ... on a supplied trace directory instead
rem   run.bat test         the full unit test suite
rem
rem Each target runs the same commands as its Makefile counterpart, with the
rem same defaults (21/9 day split, intervals theta). Preconditions are checked
rem first so a missing piece prints a message, not a traceback.
rem Override the interpreter with:  set PY=py -3

setlocal EnableExtensions
cd /d "%~dp0"

if not defined PY set "PY=python"
set "OBS_DAYS=21"
set "EVAL_DAYS=9"
set "DEMO_OUT=demo_out"
set "DEMO_SEED=42"
if not defined DEMO_INPUT set "DEMO_INPUT=data\demo_input"
set "THETA_METHOD=intervals"
set "RUN_LENGTH=12"
set "CLUSTER=rightsizing"
set "IMAGE=rightsizing/replay:latest"

if /i "%~1"=="demo-vm"   goto demo_vm
if /i "%~1"=="demo-kind" goto demo_kind
if /i "%~1"=="demo"      goto demo
if /i "%~1"=="test"      goto test
echo usage: run.bat demo-vm ^| demo-kind ^| demo [TRACE_DIR] ^| test
exit /b 2


rem ---------------------------------------------------------------- checks
:check_python
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer was not found as "%PY%".
    echo        Install it from python.org and tick "Add python.exe to PATH",
    echo        or point run.bat at another interpreter:  set PY=py -3
    exit /b 1
)
%PY% -c "import pandas, numpy, scipy, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo ERROR: missing Python packages. This needs pandas, numpy, scipy and matplotlib.
    echo        Install them with:  %PY% -m pip install -r requirements.txt
    exit /b 1
)
exit /b 0

rem Usage: call :check_traces DIR LABEL -- the folder exists and holds CSVs.
:check_traces
if not exist "%~1\" (
    echo ERROR: dataset missing -- folder %~1 does not exist.
    echo        %~2
    exit /b 1
)
dir /b /a-d "%~1\*.csv" >nul 2>&1
if errorlevel 1 (
    echo ERROR: input folder %~1 is empty -- it has no .csv files.
    echo        %~2
    exit /b 1
)
exit /b 0


rem --------------------------------------------------------------- demo-vm
:demo_vm
call :check_python || exit /b 1
call :check_traces "%DEMO_INPUT%" "Copy the real trace CSVs into it, e.g. 1007.csv 332.csv 1226.csv." || exit /b 1
if exist "%DEMO_OUT%\vm" rmdir /s /q "%DEMO_OUT%\vm"
%PY% src\demo_vm.py --input "%DEMO_INPUT%" --out "%DEMO_OUT%\vm" --obs-days %OBS_DAYS% --eval-days %EVAL_DAYS%
exit /b %errorlevel%


rem ------------------------------------------------------------- demo-kind
:demo_kind
where docker >nul 2>&1 || (
    echo ERROR: docker was not found on PATH. Install Docker Desktop.
    exit /b 1
)
where kubectl >nul 2>&1 || (
    echo ERROR: kubectl was not found on PATH. It ships with Docker Desktop.
    exit /b 1
)
docker info >nul 2>&1 || (
    echo ERROR: Docker is not running. Start Docker Desktop, wait until it
    echo        reports "Engine running", then run this again.
    exit /b 1
)
docker inspect %CLUSTER%-control-plane >nul 2>&1 || (
    echo ERROR: the kind cluster "%CLUSTER%" does not exist.
    echo        Create it with:  kind create cluster --config testbed/kind-config.yaml
    exit /b 1
)
kubectl --context kind-%CLUSTER% get nodes --request-timeout=15s >nul 2>&1 || (
    echo ERROR: the kind cluster "%CLUSTER%" is not reachable. If Docker was just
    echo        restarted, give the node a minute; otherwise:  docker start %CLUSTER%-control-plane
    exit /b 1
)
docker exec %CLUSTER%-control-plane crictl inspecti docker.io/%IMAGE% >nul 2>&1 || (
    echo ERROR: image %IMAGE% is not loaded into the cluster. Build and load it with:
    echo        docker build -t %IMAGE% testbed/
    echo        kind load docker-image %IMAGE% --name %CLUSTER%
    exit /b 1
)
set "KC=kubectl --context kind-%CLUSTER%"
%KC% delete -f testbed\demo\oom-demo.yaml --ignore-not-found --wait=true || exit /b 1
%KC% apply -f testbed\demo\oom-demo.yaml || exit /b 1
%KC% wait pod/rs-demo-oom --timeout=120s "--for=jsonpath={.status.containerStatuses[0].state.terminated.reason}=OOMKilled"
if errorlevel 1 (
    echo ERROR: the pod did not reach OOMKilled within 120 s. Current state:
    %KC% get pods -l app=rightsizing-demo
    exit /b 1
)
%KC% get pods -l app=rightsizing-demo
exit /b %errorlevel%


rem ------------------------------------------------------------------ demo
rem Writes only under DEMO_OUT, never results\, so a synthetic number can
rem never land beside a real one.
:demo
call :check_python || exit /b 1
if not "%~2"=="" goto demo_supplied
set "T=%DEMO_OUT%\traces"
set "TITLE=SYNTHETIC DEMO -- frontier on 60 generated VMs (seed %DEMO_SEED%), not a result"
set "SRC=Traces: synthetic, src/make_synthetic_traces.py --seed %DEMO_SEED%. No real data."
goto demo_run

:demo_supplied
call :check_traces "%~2" "Pass a folder of per-VM trace CSVs, or omit it for synthetic traces." || exit /b 1
set "T=%~f2"
set "TITLE=DEMO -- frontier on supplied traces (%~nx2)"
set "SRC=This trace was graciously provided by Bitbrains IT Services Inc. Supplied at demo time."

:demo_run
for /f %%s in ('%PY% -c "import time; print(int(time.time()))"') do set "START=%%s"
if exist "%DEMO_OUT%" rmdir /s /q "%DEMO_OUT%"
mkdir "%DEMO_OUT%"
if "%~2"=="" (
    %PY% src\make_synthetic_traces.py --vms 60 --days 30 --seed %DEMO_SEED% --out "%T%" || exit /b 1
)
%PY% src\demo_guard.py --traces "%T%" || exit /b 1
%PY% src\baselines.py --traces "%T%" --out "%DEMO_OUT%\baselines" --obs-days %OBS_DAYS% --eval-days %EVAL_DAYS% || exit /b 1
%PY% src\sweep_eps.py --traces "%T%" --out "%DEMO_OUT%\frontier" --obs-days %OBS_DAYS% --eval-days %EVAL_DAYS% --theta-method %THETA_METHOD% --run-length %RUN_LENGTH% || exit /b 1
%PY% src\plot_frontier.py --frontier "%DEMO_OUT%\frontier\frontier.csv" --out "%DEMO_OUT%" --name frontier_demo --series-label "M(eps), per-VM theta (%THETA_METHOD%)" --title "%TITLE%" --source "%SRC%" || exit /b 1
for /f %%s in ('%PY% -c "import time; print(int(time.time()) - %START%)"') do set "ELAPSED=%%s"
echo.
echo demo wall clock: %ELAPSED% s (target: under 120 s)
exit /b 0


rem ------------------------------------------------------------------ test
rem Same files, same order as `make test`; stops at the first failing file.
:test
call :check_python || exit /b 1
pushd tests
for %%f in (test_baselines test_replay_agent test_evt test_hypotheses test_bootstrap test_multisplit test_demo_guard test_demo_vm) do (
    echo === %%f
    %PY% %%f.py || (
        echo.
        echo FAILED: tests\%%f.py
        popd
        exit /b 1
    )
)
popd
echo.
echo all test files passed
exit /b 0
