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

package nextflow.biofuse

import groovy.transform.CompileStatic
import nextflow.Session
import nextflow.trace.TraceObserverFactoryV2
import nextflow.trace.TraceObserverV2

/**
 * Creates the {@link BiofuseObserver} when the {@code biofuse} scope enables the plugin. A config
 * error is raised here, while the session is initialised, before anything is mounted.
 */
@CompileStatic
class BiofuseObserverFactory implements TraceObserverFactoryV2 {

    @Override
    Collection<TraceObserverV2> create(Session session) {
        final config = new BiofuseConfig(session.config.navigate('biofuse') as Map)
        return config.enabled
            ? List.<TraceObserverV2>of(new BiofuseObserver(config))
            : List.<TraceObserverV2>of()
    }
}
