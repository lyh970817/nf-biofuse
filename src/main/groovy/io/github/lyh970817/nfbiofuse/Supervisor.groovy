/*
 * Copyright 2026, the nf-biofuse authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package io.github.lyh970817.nfbiofuse

import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

import groovy.json.JsonSlurper
import groovy.transform.CompileStatic
import groovy.util.logging.Slf4j
import nextflow.exception.AbortOperationException

/**
 * The plugin's handle on one supervisor process.
 *
 * The supervisor ({@code nf-biofuse-supervisor serve}) owns the mount: it mounts the view, writes the
 * manifest, reports readiness in a JSON file and serves until it receives {@code SIGTERM} or the
 * Nextflow JVM it watches is gone, and then tears everything down. It leaves the JVM's session at
 * start, so a Ctrl-C or a hangup aimed at Nextflow never reaches it; only {@link #stop()} (or the
 * JVM's death) ends it. The plugin therefore only starts it, waits for its report and stops it.
 */
@Slf4j
@CompileStatic
class Supervisor {

    static final String LOG_NAME = '.nf-biofuse.log'
    static final long POLL_MILLIS = 100
    static final long PROGRESS_MILLIS = 30_000
    /* the supervisor's teardown caps every step; this outlasts its worst case with room to spare */
    static final long STOP_TIMEOUT_MILLIS = 120_000
    static final int LOG_TAIL_LINES = 40

    private final List<String> command
    private final Path launchDir
    private final Path readyFile
    private final Path logFile
    private volatile Process process
    private volatile boolean stopRequested
    private Integer exitStatus

    Supervisor(BiofuseConfig config, Path launchDir, long watchPid) {
        this.launchDir = launchDir
        this.logFile = launchDir.resolve(LOG_NAME)
        // A fresh name the supervisor creates atomically; the file itself must not exist yet.
        this.readyFile = Files.createTempFile('nf-biofuse-', '.ready.json')
        Files.delete(readyFile)
        this.command = config.supervisorCommand(readyFile, watchPid)
    }

    List<String> getCommand() { command }

    Path getLogFile() { logFile }

    Path getReadyFile() { readyFile }

    Integer getExitStatus() { exitStatus }

    /**
     * Start the supervisor detached from the JVM's standard streams: its output goes to
     * {@link #LOG_NAME} in the launch directory, which it can keep writing after the JVM is gone.
     */
    synchronized void start() {
        if( stopRequested )
            throw new AbortOperationException('nf-biofuse: the run ended before the supervisor was started')
        log.debug "nf-biofuse: supervisor command: ${command.join(' ')}"
        final builder = new ProcessBuilder(command)
            .directory(launchDir.toFile())
            .redirectInput(ProcessBuilder.Redirect.from(new File('/dev/null')))
            .redirectOutput(ProcessBuilder.Redirect.to(logFile.toFile()))
            .redirectErrorStream(true)
        try {
            process = builder.start()
        }
        catch( IOException e ) {
            throw new AbortOperationException("nf-biofuse: cannot start the supervisor `${command[0]}`: ${e.message}. Set `biofuse.supervisor` to the `nf-biofuse-supervisor` of the environment created from supervisor/environment.yml")
        }
        log.debug "nf-biofuse: supervisor started, pid ${process.pid()}"
    }

    /**
     * Block until the supervisor reports the view ready, and return its report. There is no overall
     * deadline: the supervisor bounds the mount readiness itself, and before that it may be digesting a
     * large store, which takes as long as reading it once does. It fails as soon as the supervisor
     * reports a failure, exits, or is stopped.
     */
    Map awaitReady() {
        final started = System.currentTimeMillis()
        long nextProgress = started + PROGRESS_MILLIS
        while( true ) {
            final report = readReport()
            if( report != null ) {
                if( report.status == 'ready' )
                    return report
                throw failure("the supervisor failed with status ${report.exit_code}: ${report.error}")
            }
            if( stopRequested )
                throw failure('the run ended before the view was ready')
            if( !process.isAlive() ) {
                // A report written just before exiting is the better explanation.
                final last = readReport()
                if( last != null && last.status != 'ready' )
                    throw failure("the supervisor failed with status ${last.exit_code}: ${last.error}")
                throw failure("the supervisor exited with status ${process.exitValue()} before the view was ready")
            }
            final now = System.currentTimeMillis()
            if( now >= nextProgress ) {
                log.info "nf-biofuse: still preparing the view after ${(now - started).intdiv(1000)}s (supervisor pid ${process.pid()}; progress in ${logFile})"
                nextProgress = now + PROGRESS_MILLIS
            }
            sleep(POLL_MILLIS)
        }
    }

    /**
     * Ask the supervisor to tear the view down and wait for it to finish. Safe to call more than once
     * and from any thread: the observer calls it when the run completes, and a JVM shutdown hook calls it
     * for exits that skip the observer.
     */
    synchronized void stop() {
        if( stopRequested )
            return
        stopRequested = true
        final current = process
        if( current == null )
            return
        if( current.isAlive() ) {
            log.debug "nf-biofuse: stopping supervisor pid ${current.pid()}"
            current.destroy()
            if( !current.waitFor(STOP_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS) ) {
                log.warn "nf-biofuse: supervisor pid ${current.pid()} is still tearing the view down after ${STOP_TIMEOUT_MILLIS.intdiv(1000)}s; see ${logFile}"
                return
            }
        }
        exitStatus = current.exitValue()
        if( exitStatus == 0 )
            log.debug "nf-biofuse: supervisor stopped cleanly\n${logTail()}"
        else
            log.warn "nf-biofuse: supervisor exited with status ${exitStatus}; see ${logFile}\n${logTail()}"
        Files.deleteIfExists(readyFile)
    }

    private Map readReport() {
        if( !Files.exists(readyFile) )
            return null
        try {
            return (Map) new JsonSlurper().parse(readyFile.toFile())
        }
        catch( Exception e ) {
            // Written by rename, so a partial file is never seen; anything else is reported as such.
            return [status: 'failed', exit_code: 'unknown', error: "unreadable readiness report ${readyFile}: ${e.message}".toString()]
        }
    }

    private AbortOperationException failure(String message) {
        new AbortOperationException("nf-biofuse: ${message}\n-- last lines of ${logFile}:\n${logTail()}")
    }

    String logTail() {
        try {
            final lines = Files.readAllLines(logFile)
            return lines.subList(Math.max(0, lines.size() - LOG_TAIL_LINES), lines.size()).join('\n')
        }
        catch( IOException e ) {
            return "(cannot read ${logFile}: ${e.message})"
        }
    }
}
