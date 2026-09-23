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

import java.nio.file.Path
import java.nio.file.Paths

import groovy.transform.CompileStatic
import groovy.transform.PackageScope
import groovy.util.logging.Slf4j
import nextflow.Session
import nextflow.exception.AbortOperationException
import nextflow.exception.AbortRunException
import nextflow.trace.TraceObserverV2

/**
 * Mounts the configured view when the run is created and tears it down when the run completes.
 *
 * {@code onFlowCreate} is the earliest hook a plugin gets in a run: Nextflow calls it from
 * {@code Session.start()}, on the main thread, before it parses the pipeline script, so before any
 * workflow code runs, the parameter validation of an nf-core pipeline included. The hook blocks until
 * the supervisor reports the view ready, so the manifest and every path it names exist by the time the
 * pipeline looks at them. {@code onFlowComplete} runs after every task has ended, on success, failure
 * or abort, and stops the supervisor; a JVM shutdown hook covers exits that skip it, and the supervisor
 * itself tears down when the JVM dies without either.
 */
@Slf4j
@CompileStatic
class BiofuseObserver implements TraceObserverV2 {

    private final BiofuseConfig config
    private final Path launchDir
    private volatile Supervisor supervisor

    BiofuseObserver(BiofuseConfig config) {
        this(config, null)
    }

    /**
     * @param launchDir where the supervisor runs and writes its log; the current directory, which is
     *      Nextflow's launch directory, when {@code null}
     */
    BiofuseObserver(BiofuseConfig config, Path launchDir) {
        this.config = config
        this.launchDir = launchDir
    }

    @PackageScope
    Supervisor getSupervisor() { supervisor }

    @Override
    void onFlowCreate(Session session) {
        try {
            supervisor = new Supervisor(config, launchDir ?: Paths.get('').toAbsolutePath(), ProcessHandle.current().pid())
            Runtime.getRuntime().addShutdownHook(new Thread({ supervisor.stop() } as Runnable, 'nf-biofuse-shutdown'))
            log.info "nf-biofuse: preparing a BioFuse view of ${config.vcz}"
            supervisor.start()
            final ready = supervisor.awaitReady()
            log.info "nf-biofuse: view ${ready.genotype_view_id} (${ready.identity_mode}) ready in ${ready.readiness_seconds}s at ${ready.mount}"
            log.info "nf-biofuse: manifest ${ready.manifest}; view record ${ready.record}"
        }
        catch( Exception e ) {
            supervisor?.stop()
            // Nextflow logs anything else an observer throws at debug level and carries on; only an
            // AbortRunException ends the run, and Nextflow prints nothing for it, so log the reason here.
            final message = e instanceof AbortOperationException ? e.message : "nf-biofuse: ${e}".toString()
            log.error(message)
            throw new AbortRunException(message, e)
        }
    }

    @Override
    void onFlowComplete() {
        supervisor?.stop()
    }
}
