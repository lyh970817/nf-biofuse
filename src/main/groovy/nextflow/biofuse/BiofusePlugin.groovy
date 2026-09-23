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
import nextflow.plugin.BasePlugin
import org.pf4j.PluginWrapper

/**
 * The nf-biofuse plugin entry point. The plugin contributes the {@code biofuse} config scope
 * ({@link BiofuseConfig}) and a trace observer ({@link BiofuseObserverFactory}) that mounts the
 * configured view before the run starts and tears it down when the run ends.
 */
@CompileStatic
class BiofusePlugin extends BasePlugin {

    BiofusePlugin(PluginWrapper wrapper) {
        super(wrapper)
    }
}
